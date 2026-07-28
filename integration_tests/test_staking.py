"""Delegated staking that shares chain fees with stakers, over JSON-RPC.

StakingDeployer deploys mock NVNM + the staking proxy in one create tx; the reward token is a
mock by default or a real address passed as the constructor arg.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
import rlp
from eth_abi.abi import encode
from eth_account import Account
from eth_contract import Contract
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_checksum_address
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD
from tempo.devnet.ports import find_free_base_ports
from web3 import AsyncWeb3, Web3

from .abi import (
    CURRENT_COMMITTEE,
    CURRENT_COMMITTEE_ADDRESS,
    FEE,
    FEE_ROUTER,
    FEE_ROUTER_FACTORY,
    STAKING,
    STAKING_DEPLOYER,
)
from .conftest import _consensus_net_supervisord, _run_devnet_init
from .network import FAUCET_PRIVATE_KEY, resolve_tempo_bin, resolve_xtask_bin
from .utils import fund, gas_cost_in_token, new_account, send_calls

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

ETHER = 10**18


def _bytecode(name):
    return json.loads((Path(__file__).parent / "artifacts" / f"{name}.json").read_text())["deployer_bytecode"]


DEPLOYER_BYTECODE = _bytecode("staking")
FACTORY_BYTECODE = _bytecode("feerouter_factory")
POOL_BYTECODE = _bytecode("swap_pool")


async def _send(w3, chain_id, signer, to, fn):
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        calls=[{"to": to, "data": fn.data}],
        gas_limit=8_000_000,
    )
    assert receipt["status"] == 1
    return receipt


class Staking:
    """A deployed staking contract + its mock tokens, bound to the node."""

    def __init__(self, w3, chain_id, address, nvnm, usd):
        self.w3, self.chain_id = w3, chain_id
        self.address, self.nvnm, self.usd = address, nvnm, usd

    async def send(self, signer, fn, to=None):
        return await _send(self.w3, self.chain_id, signer, to or self.address, fn)

    async def call(self, fn, to=None, **kwargs):
        return await fn.call(self.w3, to=to or self.address, **kwargs)

    async def stake(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.nvnm)
        await self.send(signer, STAKING.fns.stake(validator, amount))

    async def deposit_reward(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.usd)
        await self.send(signer, STAKING.fns.depositReward(validator, amount))


async def _create(w3, chain_id, deployer, data):
    """Send a create tx and return the new contract's address."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=deployer.key.hex(),
        calls=[{"to": None, "data": data}],
        gas_limit=25_000_000,
    )
    assert receipt["status"] == 1, "create reverted"
    return Web3.to_checksum_address(receipt["contractAddress"])


async def _deploy(w3, chain_id, deployer, reward_token=None):
    """Deploy via StakingDeployer; ``deployer`` owns and is pre-funded. ``reward_token=None`` → mock."""
    arg = encode(["address"], [reward_token or "0x" + "00" * 20]).hex()
    d = await _create(w3, chain_id, deployer, DEPLOYER_BYTECODE + arg)
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


async def test_compound_reward_grows_stakes_pro_rata(staking, funded_account):
    """Compounded NVNM (e.g. fee-buyback proceeds) raises every delegator's stake, not shares."""
    me, val = funded_account.address, new_account().address
    await staking.stake(funded_account, val, 100 * ETHER)

    await staking.send(funded_account, ERC20.fns.approve(staking.address, 50 * ETHER), to=staking.nvnm)
    await staking.send(funded_account, STAKING.fns.compoundReward(val, 50 * ETHER))
    assert await staking.call(STAKING.fns.stakedOf(val, me)) == 150 * ETHER
    assert await staking.call(STAKING.fns.earned(val, me)) == 0  # stablecoin accumulator untouched


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


async def _router_setup(w3, chain_id, owner, commission_bps, buyback_bps):
    """PATH_USD staking + a factory (cap 100%) + one router; returns (staking, val, operator, factory, router)."""
    staking = await _deploy(w3, chain_id, owner, reward_token=PATH_USD)
    val, operator = new_account().address, new_account().address
    arg = encode(["address", "address", "uint256"], [staking.address, owner.address, 10_000]).hex()
    factory = await _create(w3, chain_id, owner, FACTORY_BYTECODE + arg)
    fn = FEE_ROUTER_FACTORY.fns.create(val, operator, commission_bps, buyback_bps)
    receipt = await staking.send(owner, fn, to=factory)
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == factory.lower())
    return staking, val, operator, factory, Web3.to_checksum_address(bytes(log["data"])[12:32])


async def test_fee_router_splits_fees_to_operator_and_stakers(w3, chain_id, funded_account):
    """PATH_USD landing on the router is split: operator commission + staking-pool deposit."""
    staking, val, operator, _, router = await _router_setup(w3, chain_id, funded_account, 1_000, 0)
    me = funded_account.address
    await staking.stake(funded_account, val, 100 * ETHER)

    # Fees arrive on the router (FeeManager's distributeFees payout is exactly this transfer).
    fees = 200 * 10**6
    await staking.send(funded_account, ERC20.fns.transfer(router, fees), to=PATH_USD)

    keeper = new_account()
    await fund(w3, keeper.address)
    await staking.send(keeper, FEE_ROUTER.fns.flush(), to=router)

    assert await staking.call(ERC20.fns.balanceOf(operator), to=PATH_USD) == fees // 10
    assert await staking.call(STAKING.fns.earned(val, me)) == fees - fees // 10
    assert await staking.call(ERC20.fns.balanceOf(router), to=PATH_USD) == 0


async def test_fee_buyback_compounds_into_stake(w3, chain_id, funded_account):
    """The buyback split buys NVNM on the owner-set market and compounds it into the pool."""
    staking, val, operator, factory, router = await _router_setup(w3, chain_id, funded_account, 1_000, 4_000)
    me = funded_account.address

    # Seed an NVNM/PATH_USD market and point the factory at it.
    pool_arg = encode(["address", "address"], [PATH_USD, staking.nvnm]).hex()
    pool = await _create(w3, chain_id, funded_account, POOL_BYTECODE + pool_arg)
    usd_reserve, nvnm_reserve = 500 * 10**6, 100 * ETHER
    await staking.send(funded_account, ERC20.fns.transfer(pool, usd_reserve), to=PATH_USD)
    await staking.send(funded_account, ERC20.fns.transfer(pool, nvnm_reserve), to=staking.nvnm)
    await staking.send(funded_account, FEE_ROUTER_FACTORY.fns.setSwapper(pool), to=factory)

    await staking.stake(funded_account, val, 100 * ETHER)
    fees = 200 * 10**6
    await staking.send(funded_account, ERC20.fns.transfer(router, fees), to=PATH_USD)
    await staking.send(funded_account, FEE_ROUTER.fns.flush(), to=router)

    buyback = fees * 4_000 // 10_000
    expected_nvnm = nvnm_reserve * buyback // (usd_reserve + buyback)  # x*y=k
    assert await staking.call(ERC20.fns.balanceOf(operator), to=PATH_USD) == fees // 10
    assert await staking.call(STAKING.fns.earned(val, me)) == fees // 2  # 50% deposited
    assert await staking.call(STAKING.fns.stakedOf(val, me)) == 100 * ETHER + expected_nvnm


async def test_distribute_fees_is_permissionless(w3, chain_id, funded_account):
    """Anyone may trigger the FeeManager's payout for any fee recipient (a router included)."""
    recipient = new_account().address  # nothing collected: succeeds as a no-op, no auth gate
    await _send(w3, chain_id, funded_account, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(recipient, PATH_USD))


# -- native epoch feed (opt-in: --consensus + a feed-capable binary) -----------------------------
# Genesis carries the precomputed proxy address; once 4 of 5 validators are staked,
# CurrentCommittee must shrink to those 4. A binary without the feed keeps all 5 and fails here.

V2_ADDRESS = to_checksum_address("0xcccccccc00000000000000000000000000000001")
_V2_VALIDATOR = (
    "(bytes32 publicKey, address validatorAddress, string ingress, string egress, "
    "address feeRecipient, uint64 index, uint64 addedAtHeight, uint64 deactivatedAtHeight)"
)
VALIDATOR_CONFIG_V2 = Contract.from_abi([f"function getActiveValidators() view returns ({_V2_VALIDATOR}[])"])

N_ELECTION_VALIDATORS = 5  # elect 4 of 5: passes the node's min(4, registry) floor


def _create_address(sender: str, nonce: int) -> str:
    """The address of a CREATE from ``sender`` at ``nonce``."""
    return to_checksum_address(keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[12:])


# Dedicated key: its nonce-0 StakingDeployer create yields the genesis-patched proxy (3rd CREATE).
ELECTION_DEPLOYER = Account.from_key(keccak(b"nvm-election-deployer"))


def _staking_genesis_address() -> str:
    return _create_address(_create_address(ELECTION_DEPLOYER.address, 0), 3)


@pytest.fixture(scope="module")
def election_net(request, tmp_path_factory):
    """A 5-validator supervisord localnet whose genesis carries `stakingElection`."""
    if not request.config.getoption("--consensus"):
        pytest.skip("staking-election feed test needs --consensus")
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("staking-election")
    config = {
        "chain_id": 1337,
        "accounts": 200,
        "epoch_length": 100,
        "seed": 0,
        "tempo_bin": resolve_tempo_bin(),
        "tempo_xtask_bin": resolve_xtask_bin(),
        "validators": [
            {"host": "127.0.0.1", "port": port, "moniker": f"node{i}"}
            for i, port in enumerate(find_free_base_ports(N_ELECTION_VALIDATORS))
        ],
        "patch_genesis": {"config": {"stakingElection": _staking_genesis_address()}},
    }
    data_dir = _run_devnet_init(base, config, gen_compose_file=False)
    yield from _consensus_net_supervisord(request, base, data_dir)


@pytest.mark.consensus
@pytest.mark.slow
async def test_distribute_fees_pays_the_collected_amount(election_net):
    """Tx fees accrue under block proposers' fee recipients; distributeFees pays them out."""
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(election_net.node_rpc_url("node0")))
    try:
        faucet = Account.from_key(FAUCET_PRIVATE_KEY)
        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=V2_ADDRESS)
        recipients = [Web3.to_checksum_address(v[4]) for v in active if int(v[4], 16) != 0]
        assert recipients, "localnet validators have no fee recipients"

        # Generate fees until some recipient has a nonzero collected balance.
        pick = collected = None
        for _ in range(10):
            await _send(w3, 1337, faucet, PATH_USD, ERC20.fns.transfer(faucet.address, 1))
            for cand in recipients:
                if c := await FEE.fns.collectedFees(cand, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS):
                    pick, collected = cand, c
                    break
            if pick:
                break
        assert pick, f"no fees accrued to any of {recipients}"

        before = await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD)
        await _send(w3, 1337, faucet, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(pick, PATH_USD))
        # More fees may accrue for `pick` between the read and the payout, so >=.
        assert await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD) - before >= collected
    finally:
        await w3.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
async def test_epoch_committee_follows_staking_election(election_net):
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(election_net.node_rpc_url("node0")))
    try:
        deployer, faucet = ELECTION_DEPLOYER, Account.from_key(FAUCET_PRIVATE_KEY)
        # Fund fresh deployer, its nonce-0 create must land on the genesis-patched address.
        await _send(w3, 1337, faucet, PATH_USD, ERC20.fns.transfer(deployer.address, 10**14))
        assert await w3.eth.get_transaction_count(deployer.address) == 0, "election deployer must be fresh"
        d = await _create(w3, 1337, deployer, DEPLOYER_BYTECODE + encode(["address"], [PATH_USD]).hex())
        cs = Web3.to_checksum_address
        staking_addr = cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d))
        assert staking_addr == _staking_genesis_address(), "proxy != genesis stakingElection address"
        nvnm = cs(await STAKING_DEPLOYER.fns.nvnm().call(w3, to=d))
        staking = Staking(w3, 1337, staking_addr, nvnm, PATH_USD)

        # Elect 4 of the 5 validators by their registry validatorAddress.
        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=V2_ADDRESS)
        assert len(active) == N_ELECTION_VALIDATORS
        key_by_addr = {cs(v[1]): bytes(v[0]) for v in active}
        elected = sorted(key_by_addr)[: N_ELECTION_VALIDATORS - 1]
        for v in elected:
            await staking.send(deployer, STAKING.fns.setCandidate(v, True))
        await staking.send(deployer, STAKING.fns.setSeatConfig(100 * ETHER, 10, 5))
        for v in elected:
            await staking.stake(deployer, v, 100 * ETHER)
        expected = {key_by_addr[v] for v in elected}

        # The DKG for a later epoch must pick up the election: CurrentCommittee shrinks to the 4.
        deadline = time.time() + 360
        last = None
        while time.time() < deadline:
            epoch, keys = await CURRENT_COMMITTEE.fns.getCommitteeMembers().call(w3, to=CURRENT_COMMITTEE_ADDRESS)
            last = (epoch, {bytes(k) for k in keys})
            if last[1] == expected:
                return
            await asyncio.sleep(3)
        block = await w3.eth.block_number
        pytest.fail(
            f"committee never matched the election (block {block}, epoch {last[0]}, "
            f"{len(last[1])} members; expected the {len(expected)} elected)"
        )
    finally:
        await w3.provider.disconnect()
