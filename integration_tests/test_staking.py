"""Delegated staking that shares chain fees with stakers, over JSON-RPC.

StakingDeployer deploys mock NVNM + the staking proxy in one create tx; the reward token is a
mock by default or a real address passed as the constructor arg.

Grouped by concern: core staking, committee election, unbonding and slashing, fee routing —
including the shared pool NVM plans to run — bridge and swapper, and the node-side epoch feed.
"""

import asyncio
import json
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_contract.erc20 import ERC20
from web3 import Web3

from .abi import (
    MOCK_ERC20,
    STAKING,
    STAKING_DEPLOYER,
)
from .utils import STATE_WRITE_GAS, fund, new_account, send_calls

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

ETHER = 10**18
cs = Web3.to_checksum_address


def _bytecode(name):
    return json.loads((Path(__file__).parent / "artifacts" / f"{name}.json").read_text())["deployer_bytecode"]


DEPLOYER_BYTECODE = _bytecode("staking")
MOCK_ERC20_BYTECODE = _bytecode("mock_erc20")


async def _send(w3, chain_id, signer, to, fn, expect=1):
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        calls=[{"to": to, "data": fn.data}],
        gas_limit=STATE_WRITE_GAS,
    )
    assert receipt["status"] == expect
    return receipt


async def _create(w3, chain_id, deployer, data):
    """Send a create tx and return the new contract's address."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=deployer.key.hex(),
        calls=[{"to": None, "data": data}],
        gas_limit=25_000_000,
    )
    if receipt["status"] != 1:
        # Create has no `to`; eth_call the initcode from the deployer to surface the revert.
        resp = await w3.provider.make_request(
            "eth_call",
            [{"from": deployer.address, "data": data}, "latest"],
        )
        err = resp.get("error") or {}
        why = f"{err.get('message', '')} {err.get('data', '') or ''}".strip() or receipt
        raise AssertionError(f"create reverted: {why}")
    return cs(receipt["contractAddress"])


class Staking:
    def __init__(self, w3, chain_id, address, nvnm, usd):
        self.w3, self.chain_id = w3, chain_id
        self.address, self.nvnm, self.usd = address, nvnm, usd

    async def send(self, signer, fn, to=None):
        return await _send(self.w3, self.chain_id, signer, to or self.address, fn)

    async def call(self, fn, to=None, **kwargs):
        return await fn.call(self.w3, to=to or self.address, **kwargs)

    async def balance(self, token, who):
        return await self.call(ERC20.fns.balanceOf(who), to=token)

    async def transfer(self, signer, token, to, amount):
        await self.send(signer, ERC20.fns.transfer(to, amount), to=token)

    async def earned(self, validator, user):
        return await self.call(STAKING.fns.earned(validator, user))

    async def staked_of(self, validator, user):
        return await self.call(STAKING.fns.stakedOf(validator, user))

    async def stake(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.nvnm)
        await self.send(signer, STAKING.fns.stake(validator, amount))

    async def deposit_reward(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.usd)
        await self.send(signer, STAKING.fns.depositReward(validator, amount))


async def _deploy(w3, chain_id, deployer, reward_token=None):
    """Deploy via StakingDeployer; ``deployer`` owns and is pre-funded.

    A zero reward token makes the deployer CREATE a second ERC-20 in the same tx as
    the staking impl + proxy. That extra create reverts on a tempo dev node (state-gas
    / create budget), so the mock USD is deployed first and passed in.
    """
    if reward_token is None:
        reward_token = await _mock_token(w3, chain_id, deployer, "nvmnUSD")
        await _send(w3, chain_id, deployer, reward_token, MOCK_ERC20.fns.mint(deployer.address, 1_000 * ETHER))
    arg = encode(["address"], [reward_token]).hex()
    d = await _create(w3, chain_id, deployer, DEPLOYER_BYTECODE + arg)
    addr, nvnm, usd = (
        cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d)),
        cs(await STAKING_DEPLOYER.fns.nvnm().call(w3, to=d)),
        cs(await STAKING_DEPLOYER.fns.usd().call(w3, to=d)),
    )
    return Staking(w3, chain_id, addr, nvnm, usd)


async def _mock_token(w3, chain_id, deployer, symbol):
    arg = encode(["string", "string"], [symbol, symbol]).hex()
    return await _create(w3, chain_id, deployer, MOCK_ERC20_BYTECODE + arg)


async def _setup_election(staking, owner, validators):
    for v in validators:
        await staking.send(owner, STAKING.fns.setCandidate(v, True))
    await staking.send(owner, STAKING.fns.setCommitteeConfig(21, 1, 0))


async def _elected(staking, **kwargs):
    """``computeCommittee()`` as checksummed validators, ready to compare directly."""
    vals = await staking.call(STAKING.fns.computeCommittee(), **kwargs)
    return [cs(a) for a in vals]


_STAKING_PROXY_NONCE = 3


@pytest.fixture
async def staking(w3, chain_id, funded_account):
    return await _deploy(w3, chain_id, funded_account)


class TestStaking:
    """Core delegation: stake, pro-rata reward share, unstake, compounding."""

    async def test_stake_deposit_claim(self, staking, funded_account):
        me, val = funded_account.address, new_account().address
        await staking.stake(funded_account, val, 100 * ETHER)
        assert await staking.staked_of(val, me) == 100 * ETHER

        await staking.deposit_reward(funded_account, val, 500 * ETHER)
        assert await staking.earned(val, me) == 500 * ETHER

        before = await staking.balance(staking.usd, me)
        await staking.send(funded_account, STAKING.fns.claim(val))
        assert await staking.balance(staking.usd, me) - before == 500 * ETHER
        assert await staking.earned(val, me) == 0

    async def test_rewards_split_pro_rata(self, staking, funded_account):
        val, bob = new_account().address, new_account()
        await fund(staking.w3, bob.address)  # gas
        await staking.transfer(funded_account, staking.nvnm, bob.address, 100 * ETHER)

        await staking.stake(funded_account, val, 300 * ETHER)
        await staking.stake(bob, val, 100 * ETHER)  # 3:1
        await staking.deposit_reward(funded_account, val, 400 * ETHER)

        assert await staking.earned(val, funded_account.address) == 300 * ETHER
        assert await staking.earned(val, bob.address) == 100 * ETHER

    async def test_unstake_returns_stake(self, staking, funded_account):
        me, val = funded_account.address, new_account().address
        await staking.stake(funded_account, val, 100 * ETHER)

        before = await staking.balance(staking.nvnm, me)
        await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))
        assert await staking.balance(staking.nvnm, me) - before == 100 * ETHER
        assert await staking.staked_of(val, me) == 0

    async def test_compound_reward_grows_stakes_pro_rata(self, staking, funded_account):
        """Compounded NVNM (e.g. fee-buyback proceeds) raises every delegator's stake, not shares."""
        me, val = funded_account.address, new_account().address
        await staking.stake(funded_account, val, 100 * ETHER)

        await staking.send(funded_account, ERC20.fns.approve(staking.address, 50 * ETHER), to=staking.nvnm)
        await staking.send(funded_account, STAKING.fns.compoundReward(val, 50 * ETHER))
        # 1 wei tolerance: the virtual-offset share rate rounds in the pool's favor.
        assert abs(await staking.staked_of(val, me) - 150 * ETHER) <= 1
        assert await staking.earned(val, me) == 0  # stablecoin accumulator untouched


class TestCommitteeElection:
    """Top-N equal-seat election and the deterministic read the node's feed relies on."""

    async def test_ranks_by_stake_one_seat_each(self, staking, funded_account):
        """Committee is top-N, one equal seat (engine is unit-weighted)."""
        v1, v2 = new_account().address, new_account().address
        await _setup_election(staking, funded_account, [v1, v2])

        await staking.stake(funded_account, v1, 300 * ETHER)
        await staking.stake(funded_account, v2, 150 * ETHER)

        assert await _elected(staking) == [v1, v2]

    async def test_bonded_candidacy_register_and_resign(self, staking, funded_account):
        """Permissionless candidacy: post the NVNM bond to register, refunded on resign."""
        me = funded_account.address
        await staking.send(funded_account, STAKING.fns.setCandidacyBond(50 * ETHER))
        await staking.send(funded_account, ERC20.fns.approve(staking.address, 50 * ETHER), to=staking.nvnm)

        before = await staking.balance(staking.nvnm, me)
        await staking.send(funded_account, STAKING.fns.registerCandidate())
        assert await staking.balance(staking.nvnm, me) == before - 50 * ETHER
        assert await staking.call(STAKING.fns.bondOf(me)) == 50 * ETHER
        assert [cs(a) for a in await staking.call(STAKING.fns.candidates())] == [me]

        await staking.send(funded_account, STAKING.fns.resignCandidate())
        assert await staking.balance(staking.nvnm, me) == before  # bond refunded
        assert list(await staking.call(STAKING.fns.candidates())) == []

    async def test_read_is_a_block_snapshot(self, staking, funded_account):
        """The node's at-hash read is itself the stake snapshot."""
        v1, v2 = new_account().address, new_account().address
        await _setup_election(staking, funded_account, [v1, v2])
        await staking.stake(funded_account, v1, 300 * ETHER)
        snapshot = await staking.w3.eth.block_number

        await staking.stake(funded_account, v2, 100 * ETHER)  # committee changes after `snapshot`

        assert await _elected(staking, block_identifier=snapshot) == [v1]
        assert await _elected(staking) == [v1, v2]

    async def test_read_from_system_caller(self, staking, funded_account):
        """From address(0), an under-staked committee decodes as empty (the fallback trigger)."""
        zero = "0x" + "00" * 20
        v = new_account().address
        await _setup_election(staking, funded_account, [v])

        assert await _elected(staking, **{"from": zero}) == []  # no qualifying stake yet

        await staking.stake(funded_account, v, 200 * ETHER)
        assert await _elected(staking, **{"from": zero}) == [v]

    async def test_read_fits_node_gas_cap(self, staking, funded_account):
        """The election read fits the node's call cap, measured by the node itself.

        The unit suite bounds the worst case (a full candidate list); this checks the real
        estimate a node produces, which is what the consensus feed is actually billed.
        """
        validators = [new_account().address for _ in range(5)]
        await _setup_election(staking, funded_account, validators)
        for v in validators:
            await staking.stake(funded_account, v, 100 * ETHER)

        gas = await staking.w3.eth.estimate_gas(
            {"from": "0x" + "00" * 20, "to": staking.address, "data": STAKING.fns.computeCommittee().data}
        )
        assert gas < 30_000_000, f"election read too expensive for the node cap: {gas}"


class TestUnbondingAndSlash:
    """The unbonding delay, and bond-only slashing that never reaches delegators."""

    async def test_unbonding_delays_withdrawal(self, staking, funded_account):
        """With a period set, unstake parks stake in a pending bucket; withdraw pays out after it."""
        me, val = funded_account.address, new_account().address
        await staking.send(funded_account, STAKING.fns.setUnbondingPeriod(2))  # 2s
        await staking.stake(funded_account, val, 100 * ETHER)

        before = await staking.balance(staking.nvnm, me)
        await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))
        assert await staking.balance(staking.nvnm, me) == before  # parked, not paid
        amount, release_at = await staking.call(STAKING.fns.pendingUnstakeOf(val, me))
        assert amount == 100 * ETHER and release_at > 0

        await asyncio.sleep(3)  # pass the unbonding period
        await staking.send(funded_account, STAKING.fns.withdraw(val))
        assert await staking.balance(staking.nvnm, me) == before + 100 * ETHER
        assert (await staking.call(STAKING.fns.pendingUnstakeOf(val, me)))[0] == 0

    async def test_slash_takes_the_bond_and_nothing_else(self, staking, funded_account):
        """Bond-only slash: the acquired bond is seized whole, and every delegated token
        survives it — live stake and the unbonding bucket alike."""
        me, treasury = funded_account.address, new_account().address
        validator = new_account()
        await fund(staking.w3, validator.address)  # gas

        # The validator posts the candidacy bond from its own account: its skin in the game.
        await staking.send(funded_account, STAKING.fns.setCandidacyBond(50 * ETHER))
        await staking.transfer(funded_account, staking.nvnm, validator.address, 50 * ETHER)
        await staking.send(validator, ERC20.fns.approve(staking.address, 50 * ETHER), to=staking.nvnm)
        await staking.send(validator, STAKING.fns.registerCandidate())

        # A delegator holding stake both live and unbonding, so the slash has both to miss.
        val = validator.address
        await staking.send(funded_account, STAKING.fns.setUnbondingPeriod(2))
        await staking.stake(funded_account, val, 200 * ETHER)
        await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))

        await staking.send(funded_account, STAKING.fns.slash(val, 10_000, treasury))
        assert await staking.balance(staking.nvnm, treasury) == 50 * ETHER, "the whole bond"
        assert await staking.call(STAKING.fns.bondOf(val)) == 0
        assert await staking.staked_of(val, me) == 100 * ETHER, "live stake untouched"
        assert (await staking.call(STAKING.fns.pendingUnstakeOf(val, me)))[0] == 100 * ETHER

        await asyncio.sleep(3)
        before = await staking.balance(staking.nvnm, me)
        await staking.send(funded_account, STAKING.fns.withdraw(val))
        assert await staking.balance(staking.nvnm, me) - before == 100 * ETHER
