"""Delegated staking that shares chain fees with stakers, over JSON-RPC.

StakingDeployer deploys mock NVNM + the staking proxy in one create tx; the reward token is a
mock by default or a real address passed as the constructor arg.
"""

import asyncio
import json
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import Web3

from .abi import STAKING, STAKING_DEPLOYER
from .utils import fund, gas_cost_in_token, new_account, send_calls

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

ETHER = 10**18

_ARTIFACT = json.loads((Path(__file__).parent / "artifacts" / "staking.json").read_text())
DEPLOYER_BYTECODE = _ARTIFACT["deployer_bytecode"]


class Staking:
    """A deployed staking contract + its mock tokens, bound to the node."""

    def __init__(self, w3, chain_id, address, nvnm, usd):
        self.w3, self.chain_id = w3, chain_id
        self.address, self.nvnm, self.usd = address, nvnm, usd

    async def send(self, signer, fn, to=None):
        receipt = await send_calls(
            self.w3,
            chain_id=self.chain_id,
            private_key=signer.key.hex(),
            calls=[{"to": to or self.address, "data": fn.data}],
            gas_limit=8_000_000,
        )
        assert receipt["status"] == 1
        return receipt

    async def call(self, fn, to=None, **kwargs):
        return await fn.call(self.w3, to=to or self.address, **kwargs)

    async def stake(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.nvnm)
        await self.send(signer, STAKING.fns.stake(validator, amount))

    async def deposit_reward(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.usd)
        await self.send(signer, STAKING.fns.depositReward(validator, amount))


async def _deploy(w3, chain_id, deployer, reward_token=None):
    """Deploy via StakingDeployer; ``deployer`` owns and is pre-funded. ``reward_token=None`` → mock."""
    arg = encode(["address"], [reward_token or "0x" + "00" * 20]).hex()
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=deployer.key.hex(),
        calls=[{"to": None, "data": DEPLOYER_BYTECODE + arg}],
        gas_limit=25_000_000,  # deploys token(s) + impl + proxy
    )
    assert receipt["status"] == 1, "deployer create reverted"
    d = receipt["contractAddress"]
    cs = Web3.to_checksum_address
    addr, nvnm, usd = (
        cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d)),
        cs(await STAKING_DEPLOYER.fns.nvnm().call(w3, to=d)),
        cs(await STAKING_DEPLOYER.fns.usd().call(w3, to=d)),
    )
    return Staking(w3, chain_id, addr, nvnm, usd)


@pytest.fixture
async def staking(w3, chain_id, funded_account):
    return await _deploy(w3, chain_id, funded_account)


async def test_stake_deposit_claim(staking, funded_account):
    me, val = funded_account.address, new_account().address
    await staking.stake(funded_account, val, 100 * ETHER)
    assert await staking.call(STAKING.fns.stakedOf(val, me)) == 100 * ETHER

    await staking.deposit_reward(funded_account, val, 500 * ETHER)
    assert await staking.call(STAKING.fns.earned(val, me)) == 500 * ETHER

    before = await staking.call(ERC20.fns.balanceOf(me), to=staking.usd)
    await staking.send(funded_account, STAKING.fns.claim(val))
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.usd) - before == 500 * ETHER
    assert await staking.call(STAKING.fns.earned(val, me)) == 0


async def test_rewards_split_pro_rata(staking, funded_account):
    val = new_account().address
    bob = new_account()
    await fund(staking.w3, bob.address)  # gas
    # Seed bob's stake from the pre-funded owner.
    await staking.send(funded_account, ERC20.fns.transfer(bob.address, 100 * ETHER), to=staking.nvnm)

    await staking.stake(funded_account, val, 300 * ETHER)
    await staking.stake(bob, val, 100 * ETHER)  # 3:1
    await staking.deposit_reward(funded_account, val, 400 * ETHER)

    assert await staking.call(STAKING.fns.earned(val, funded_account.address)) == 300 * ETHER
    assert await staking.call(STAKING.fns.earned(val, bob.address)) == 100 * ETHER


async def test_unstake_returns_stake(staking, funded_account):
    me, val = funded_account.address, new_account().address
    await staking.stake(funded_account, val, 100 * ETHER)

    before = await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm)
    await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm) - before == 100 * ETHER
    assert await staking.call(STAKING.fns.stakedOf(val, me)) == 0


async def test_committee_election_quantizes_stake_into_seats(staking, funded_account):
    """Weighted-PoS selection: seats = min(stake/tokensPerSeat, cap), ranked by stake."""
    v1, v2 = new_account().address, new_account().address
    await staking.send(funded_account, STAKING.fns.setCandidate(v1, True))
    await staking.send(funded_account, STAKING.fns.setCandidate(v2, True))
    await staking.send(funded_account, STAKING.fns.setSeatConfig(100 * ETHER, 10, 5))

    await staking.stake(funded_account, v1, 300 * ETHER)  # 3 seats
    await staking.stake(funded_account, v2, 150 * ETHER)  # 1 seat (floors)

    vals, seats = await staking.call(STAKING.fns.computeCommittee())
    assert [Web3.to_checksum_address(a) for a in vals] == [v1, v2]  # ranked by stake desc
    assert list(seats) == [3, 1]


async def test_bonded_candidacy_register_and_resign(staking, funded_account):
    """Permissionless candidacy: post the NVNM bond to register, refunded on resign."""
    me = funded_account.address
    await staking.send(funded_account, STAKING.fns.setCandidacyBond(50 * ETHER))
    await staking.send(funded_account, ERC20.fns.approve(staking.address, 50 * ETHER), to=staking.nvnm)

    before = await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm)
    await staking.send(funded_account, STAKING.fns.registerCandidate())
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm) == before - 50 * ETHER
    assert await staking.call(STAKING.fns.bondOf(me)) == 50 * ETHER
    cands = await staking.call(STAKING.fns.candidates())
    assert [Web3.to_checksum_address(a) for a in cands] == [me]

    await staking.send(funded_account, STAKING.fns.resignCandidate())
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm) == before  # bond refunded
    assert list(await staking.call(STAKING.fns.candidates())) == []


async def test_unbonding_delays_withdrawal(staking, funded_account):
    """With a period set, unstake parks stake in a pending bucket; withdraw pays out after it."""
    me, val = funded_account.address, new_account().address
    await staking.send(funded_account, STAKING.fns.setUnbondingPeriod(2))  # 2s
    await staking.stake(funded_account, val, 100 * ETHER)

    before = await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm)
    await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm) == before  # parked, not paid
    amount, release_at = await staking.call(STAKING.fns.pendingUnstakeOf(val, me))
    assert amount == 100 * ETHER and release_at > 0

    await asyncio.sleep(3)  # pass the unbonding period
    await staking.send(funded_account, STAKING.fns.withdraw(val))
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm) == before + 100 * ETHER
    assert (await staking.call(STAKING.fns.pendingUnstakeOf(val, me)))[0] == 0


# -- consensus-read contract ---------------------------------------------------------------------
# The node's staking-election feed eth_calls computeCommittee() at a block hash from address(0)
# and expects deterministic, snapshot-consistent results.


async def _setup_election(staking, owner, validators, tokens_per_seat=100 * ETHER):
    for v in validators:
        await staking.send(owner, STAKING.fns.setCandidate(v, True))
    await staking.send(owner, STAKING.fns.setSeatConfig(tokens_per_seat, 10, 5))


async def test_committee_read_is_a_block_snapshot(staking, funded_account):
    """The node's at-hash read is itself the stake snapshot."""
    v1, v2 = new_account().address, new_account().address
    await _setup_election(staking, funded_account, [v1, v2])
    await staking.stake(funded_account, v1, 300 * ETHER)
    snapshot = await staking.w3.eth.block_number

    await staking.stake(funded_account, v2, 100 * ETHER)  # committee changes after `snapshot`

    then_vals, then_seats = await staking.call(STAKING.fns.computeCommittee(), block_identifier=snapshot)
    now_vals, now_seats = await staking.call(STAKING.fns.computeCommittee())
    cs = Web3.to_checksum_address
    assert ([cs(a) for a in then_vals], list(then_seats)) == ([v1], [3])
    assert ([cs(a) for a in now_vals], list(now_seats)) == ([v1, v2], [3, 1])


async def test_committee_read_from_system_caller(staking, funded_account):
    """From address(0), an under-staked committee decodes as empty (the fallback trigger)."""
    zero = "0x" + "00" * 20
    v = new_account().address
    await _setup_election(staking, funded_account, [v])

    vals, seats = await staking.call(STAKING.fns.computeCommittee(), **{"from": zero})
    assert (list(vals), list(seats)) == ([], [])  # no qualifying stake yet

    await staking.stake(funded_account, v, 200 * ETHER)
    vals, seats = await staking.call(STAKING.fns.computeCommittee(), **{"from": zero})
    assert ([Web3.to_checksum_address(a) for a in vals], list(seats)) == ([v], [2])


async def test_committee_read_fits_node_gas_cap(staking, funded_account):
    """The election read must fit the node's 30M-gas call cap."""
    validators = [new_account().address for _ in range(5)]
    await _setup_election(staking, funded_account, validators)
    for v in validators:
        await staking.stake(funded_account, v, 100 * ETHER)

    gas = await staking.w3.eth.estimate_gas(
        {"from": "0x" + "00" * 20, "to": staking.address, "data": STAKING.fns.computeCommittee().data}
    )
    assert gas < 30_000_000, f"election read too expensive for the node cap: {gas}"


async def test_slash_cuts_live_and_pending_stake(staking, funded_account):
    """Governance slash: live stake halves pro-rata; unbonding stake is cut on withdraw too."""
    me, val, treasury = funded_account.address, new_account().address, new_account().address
    await staking.send(funded_account, STAKING.fns.setUnbondingPeriod(2))
    await staking.stake(funded_account, val, 200 * ETHER)
    await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))  # half live, half pending

    # Owner (Safe stand-in) slashes 50%: seizes 50 live + 50 pending.
    await staking.send(funded_account, STAKING.fns.slash(val, 5_000, treasury))
    assert await staking.call(ERC20.fns.balanceOf(treasury), to=staking.nvnm) == 100 * ETHER
    assert await staking.call(STAKING.fns.stakedOf(val, me)) == 50 * ETHER
    assert (await staking.call(STAKING.fns.pendingUnstakeOf(val, me)))[0] == 50 * ETHER

    await asyncio.sleep(3)
    before = await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm)
    await staking.send(funded_account, STAKING.fns.withdraw(val))
    assert await staking.call(ERC20.fns.balanceOf(me), to=staking.nvnm) - before == 50 * ETHER  # net of slash


async def test_rewards_paid_in_real_fee_stablecoin(w3, chain_id, funded_account):
    """The production reward leg: the contract pulls and pays out PATH_USD (TIP-20 precompile)."""
    staking = await _deploy(w3, chain_id, funded_account, reward_token=PATH_USD)
    assert staking.usd == Web3.to_checksum_address(PATH_USD)

    me, val = funded_account.address, new_account().address
    await staking.stake(funded_account, val, 100 * ETHER)

    reward = 400 * 10**6  # PATH_USD base units (faucet funds far more)
    await staking.deposit_reward(funded_account, val, reward)
    assert await staking.call(STAKING.fns.earned(val, me)) == reward

    before = await staking.call(ERC20.fns.balanceOf(me), to=PATH_USD)
    receipt = await staking.send(funded_account, STAKING.fns.claim(val))
    # The claim tx's own gas is also paid in PATH_USD, so net it out of the delta.
    assert await staking.call(ERC20.fns.balanceOf(me), to=PATH_USD) - before == reward - gas_cost_in_token(receipt)
