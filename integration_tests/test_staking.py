"""Delegated staking that shares chain fees with stakers, over JSON-RPC.

StakingDeployer deploys mock NVNM + the staking proxy in one create tx; the reward token is a
mock by default or a real address passed as the constructor arg.

Grouped by concern: core staking, committee election, unbonding and slashing, fee routing —
including the shared pool NVM plans to run — bridge and swapper, and the node-side epoch feed.
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
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS
from tempo.devnet.ports import find_free_base_ports
from web3 import AsyncWeb3, Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .abi import (
    BRIDGED_NVNM,
    CURRENT_COMMITTEE,
    CURRENT_COMMITTEE_ADDRESS,
    FEE,
    FEE_ROUTER,
    FEE_ROUTER_FACTORY,
    GUARDED_SWAPPER,
    MOCK_ERC20,
    STAKING,
    STAKING_DEPLOYER,
    VALIDATOR_CONFIG_V2,
)
from .conftest import _consensus_net_supervisord, _run_devnet_init
from .network import FAUCET_PRIVATE_KEY, resolve_tempo_bin, resolve_xtask_bin
from .utils import STATE_WRITE_GAS, call_revert, fund, gas_cost_in_token, new_account, send_calls

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

ETHER = 10**18
cs = Web3.to_checksum_address

# Custom-error selector (keccak256(sig)[:4]) for revert-reason assertions.
ERR_AMOUNT_TOO_LARGE = "0x" + keccak(text="AmountTooLarge()")[:4].hex()

# FeeRouterFactory.RouterCreated, for picking it out of a batch that also logs the factory's own
# constructor events.
ROUTER_CREATED_TOPIC = keccak(text="RouterCreated(address,address,address,uint256)")

# The global fee pool's sentinel "validator": every staker delegates here and every validator's
# router deposits here, so the pool key belongs to no real operator.
GLOBAL_POOL = cs(keccak(b"nvm.global.pool")[12:])


def _bytecode(name):
    return json.loads((Path(__file__).parent / "artifacts" / f"{name}.json").read_text())["deployer_bytecode"]


DEPLOYER_BYTECODE = _bytecode("staking")
FACTORY_BYTECODE = _bytecode("feerouter_factory")
ROUTER_BYTECODE = _bytecode("feerouter")  # for predicting the factory's CREATE2 address
POOL_BYTECODE = _bytecode("swap_pool")
MOCK_ERC20_BYTECODE = _bytecode("mock_erc20")
BRIDGED_NVNM_BYTECODE = _bytecode("bridged_nvnm")
GUARDED_SWAPPER_BYTECODE = _bytecode("guarded_swapper")


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


async def _router_setup(w3, chain_id, owner, commission_bps, *, split=False):
    """PATH_USD staking, a factory over it (commission cap 100%), and one router.

    `split` applies the Phase 1 protocol cuts (25 devshare / 25 buybacks) so a test can assert
    the whole waterfall; the two cut recipients come back either way.
    """
    staking = await _deploy(w3, chain_id, owner, reward_token=PATH_USD)
    validator, operator = new_account().address, new_account().address
    arg = encode(["address", "address", "uint256"], [staking.address, owner.address, 10_000]).hex()
    factory = await _create(w3, chain_id, owner, FACTORY_BYTECODE + arg)

    receipt = await staking.send(owner, FEE_ROUTER_FACTORY.fns.create(validator, operator, commission_bps), to=factory)
    log = next(lg for lg in receipt["logs"] if bytes(lg["topics"][0]) == ROUTER_CREATED_TOPIC)
    router = cs(bytes(log["data"])[12:32])

    treasury, buybacks = new_account().address, new_account().address
    if split:
        split_fn = FEE_ROUTER_FACTORY.fns.setProtocolSplit(treasury, buybacks, 2_500, 2_500)
        await staking.send(owner, split_fn, to=factory)
    return staking, validator, operator, router, treasury, buybacks


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


# Routers cost ~4.5M gas each to deploy, against a 30M per-tx cap; three fit beside the factory.
ROUTERS_PER_TX = 3


def _create_address(sender: str, nonce: int) -> str:
    """The address of a CREATE from ``sender`` at ``nonce``."""
    return cs(keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[12:])


# StakingDeployer's CREATEs, from a contract's starting nonce of 1: mock NVNM, staking impl,
# ERC-1967 proxy. Only holds when a real reward token is passed — a zero one mints a mock USD
# too and shifts these by one. test_committee_follows_staking_election asserts the deployed
# proxy against this, so a drifting sequence fails there rather than silently.
_STAKING_NVNM_NONCE = 1
_STAKING_PROXY_NONCE = 3


def _staking_deploys_from(sender: str, nonce: int) -> tuple[str, str]:
    """The (mock NVNM, staking proxy) a StakingDeployer created at ``sender``'s ``nonce`` makes."""
    deployer = _create_address(sender, nonce)
    return _create_address(deployer, _STAKING_NVNM_NONCE), _create_address(deployer, _STAKING_PROXY_NONCE)


def _router_address(factory: str, validator: str, operator: str, commission_bps: int, staking: str) -> str:
    """The CREATE2 address `FeeRouterFactory.create` will deploy to.

    Lets the create and the calls that need its address ride in one tx. The salt and
    constructor args mirror the factory; a drift shows up as the RouterCreated log in that
    same receipt disagreeing, which the caller asserts.
    """
    salt = keccak(encode(["address", "address", "uint256"], [validator, operator, commission_bps]))
    initcode = bytes.fromhex(ROUTER_BYTECODE.removeprefix("0x")) + encode(
        ["address", "address", "address", "address", "uint256"],
        [validator, operator, staking, factory, commission_bps],
    )
    return cs(keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + keccak(initcode))[12:])


async def _deploy_stack(w3, chain_id, deployer, *, routers, commission_bps, stake, split=None, validators=None):
    """Stand the fee stack up in three txs: one router per operator, all keyed to `GLOBAL_POOL`.

    Every delegator share then lands in the one pool a staker holds shares in, whichever
    validator earned it. `split` applies the protocol cuts; `validators` repoints a registry's
    fee recipients at the routers, one each.

    Three is a floor, not a choice: a 0x76 takes at most one CREATE and only as its first call,
    so StakingDeployer and the factory cannot share a tx, and the 30M gas cap fits only three
    routers at ~4.5M each. Nothing waits on a receipt — CREATE and CREATE2 addresses are both
    predictable, so the wiring batches with the deploys.
    """
    nonce = await w3.eth.get_transaction_count(deployer.address)
    nvnm, staking = _staking_deploys_from(deployer.address, nonce)
    factory = _create_address(deployer.address, nonce + 1)
    payouts = [new_account().address for _ in range(routers)]
    addresses = [_router_address(factory, GLOBAL_POOL, p, commission_bps, staking) for p in payouts]
    assert len(addresses) <= 2 * ROUTERS_PER_TX, "more routers than this three-tx shape can deploy"

    async def send(calls):
        receipt = await send_calls(
            w3,
            chain_id=chain_id,
            private_key=deployer.key.hex(),
            calls=calls,
            gas_limit=30_000_000,
        )
        assert receipt["status"] == 1, "deploy step reverted"
        return receipt

    # tx 1 — StakingDeployer: the mock NVNM, the impl, the proxy, and a stake balance for us.
    await send([{"to": None, "data": DEPLOYER_BYTECODE + encode(["address"], [PATH_USD]).hex()}])

    creates = [
        {"to": factory, "data": FEE_ROUTER_FACTORY.fns.create(GLOBAL_POOL, p, commission_bps).data} for p in payouts
    ]
    factory_create = {
        "to": None,
        "data": FACTORY_BYTECODE + encode(["address", "address", "uint256"], [staking, deployer.address, 10_000]).hex(),
    }

    # tx 2 — the factory, plus as many routers as fit beside it.
    first = await send([factory_create, *creates[:ROUTERS_PER_TX]])
    # tx 3 — the rest of the routers, then the wiring, which is all cheap calls.
    rest = await send(
        [
            *creates[ROUTERS_PER_TX:],
            *(
                [{"to": factory, "data": FEE_ROUTER_FACTORY.fns.setProtocolSplit(*split, 2_500, 2_500).data}]
                if split
                else []
            ),
            *(
                {"to": VALIDATOR_CONFIG_V2_ADDRESS, "data": VALIDATOR_CONFIG_V2.fns.setFeeRecipient(v[5], r).data}
                for v, r in zip(validators or [], addresses, strict=bool(validators))
            ),
            {"to": nvnm, "data": ERC20.fns.approve(staking, stake).data},
            {"to": staking, "data": STAKING.fns.stake(GLOBAL_POOL, stake).data},
        ]
    )

    # Match the topic, not the emitter: the factory's constructor logs into this batch too.
    logged = [
        cs(bytes(lg["data"])[12:32])
        for receipt in (first, rest)
        for lg in receipt["logs"]
        if bytes(lg["topics"][0]) == ROUTER_CREATED_TOPIC
    ]
    assert logged == addresses, "predicted router addresses drifted from the factory"
    return Staking(w3, chain_id, staking, nvnm, PATH_USD), addresses, payouts


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


class TestFeeRouting:
    """FeeRouter: protocol cuts then validator remainder → commission + delegators."""

    async def test_rewards_paid_in_real_fee_stablecoin(self, w3, chain_id, funded_account):
        """The production reward leg: the contract pulls and pays out PATH_USD (TIP-20 precompile)."""
        staking = await _deploy(w3, chain_id, funded_account, reward_token=PATH_USD)
        assert staking.usd == cs(PATH_USD)

        me, val = funded_account.address, new_account().address
        await staking.stake(funded_account, val, 100 * ETHER)

        reward = 400 * 10**6  # PATH_USD base units (faucet funds far more)
        await staking.deposit_reward(funded_account, val, reward)
        assert await staking.earned(val, me) == reward

        before = await staking.balance(PATH_USD, me)
        receipt = await staking.send(funded_account, STAKING.fns.claim(val))
        # The claim tx's own gas is also paid in PATH_USD, so net it out of the delta.
        assert await staking.balance(PATH_USD, me) - before == reward - gas_cost_in_token(receipt)

    async def test_router_splits_fees_to_operator_and_stakers(self, w3, chain_id, funded_account):
        """PATH_USD landing on the router is split: operator commission + staking-pool deposit."""
        staking, val, operator, router, _, _ = await _router_setup(w3, chain_id, funded_account, 1_000)
        me = funded_account.address
        await staking.stake(funded_account, val, 100 * ETHER)

        # Fees arrive on the router (FeeManager's distributeFees payout is exactly this transfer).
        fees = 200 * 10**6
        await staking.transfer(funded_account, PATH_USD, router, fees)

        keeper = new_account()
        await fund(w3, keeper.address)
        await staking.send(keeper, FEE_ROUTER.fns.flush(), to=router)

        assert await staking.balance(PATH_USD, operator) == fees // 10
        assert await staking.earned(val, me) == fees - fees // 10
        assert await staking.balance(PATH_USD, router) == 0

    async def test_protocol_split_pays_devshare_and_buyback(self, w3, chain_id, funded_account):
        """25/25 protocol cuts; validator remainder goes to the operator when the pool is empty."""
        setup = await _router_setup(w3, chain_id, funded_account, 10_000, split=True)
        staking, val, operator, router, treasury, buybacks = setup

        fees = 200 * 10**6
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(funded_account, FEE_ROUTER.fns.flush(), to=router)

        assert await staking.balance(PATH_USD, treasury) == fees // 4
        assert await staking.balance(PATH_USD, buybacks) == fees // 4
        assert await staking.balance(PATH_USD, operator) == fees // 2
        assert await staking.earned(val, funded_account.address) == 0

    async def test_flush_waterfalls_a_second_fee_token(self, w3, chain_id, funded_account):
        """A token other than the pool's reward token still gets the cuts, and its delegator
        share is escrowed — including against a second, permissionless flush.

        Not the steady state: FeeManager swaps each payer's fee into the recipient's preferred
        token, so a router normally holds one. This is the misconfigured-or-donated case.
        """
        setup = await _router_setup(w3, chain_id, funded_account, 1_000, split=True)
        staking, val, operator, router, treasury, buybacks = setup
        await staking.stake(funded_account, val, 100 * ETHER)

        other = await _mock_token(w3, chain_id, funded_account, "otherUSD")
        fees = 100 * ETHER
        await _send(w3, chain_id, funded_account, other, MOCK_ERC20.fns.mint(router, fees))
        await staking.send(funded_account, FEE_ROUTER.fns.flush(other), to=router)

        held = fees // 2 - fees // 20  # the validator remainder less the 10% commission
        assert await staking.balance(other, treasury) == fees // 4
        assert await staking.balance(other, buybacks) == fees // 4
        assert await staking.balance(other, operator) == fees // 20
        assert await staking.balance(other, router) == held
        assert await staking.call(FEE_ROUTER.fns.heldForDelegators(other), to=router) == held
        assert await staking.earned(val, funded_account.address) == 0

        keeper = new_account()
        await fund(w3, keeper.address)
        await staking.send(keeper, FEE_ROUTER.fns.flush(other), to=router)
        assert await staking.balance(other, treasury) == fees // 4, "devshare not taken twice"
        assert await staking.balance(other, router) == held, "delegators' share intact"

    async def test_distribute_fees_is_permissionless(self, w3, chain_id, funded_account):
        """Anyone may trigger the FeeManager's payout for any fee recipient (a router included)."""
        recipient = new_account().address  # nothing collected: succeeds as a no-op, no auth gate
        await _send(w3, chain_id, funded_account, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(recipient, PATH_USD))

    async def test_all_validators_fees_reach_one_staker_pool(self, w3, chain_id, funded_account):
        """Two validators, two routers, one pool: a staker who picked no operator earns from both."""
        owner = funded_account
        # The same stack the localnet deploys, minus a registry to repoint and the protocol
        # cuts: two routers keyed to the shared pool, each paying its own operator.
        staking, routers, payouts = await _deploy_stack(
            w3, chain_id, owner, routers=2, commission_bps=1_000, stake=100 * ETHER
        )

        fees = 200 * 10**6
        for router in routers:
            await staking.transfer(owner, PATH_USD, router, fees)
            await staking.send(owner, FEE_ROUTER.fns.flush(), to=router)

        # Both validators' deposits land in the single pool the staker holds shares in.
        assert await staking.earned(GLOBAL_POOL, owner.address) == 2 * (fees - fees // 10)
        for payout in payouts:
            assert await staking.balance(PATH_USD, payout) == fees // 10


class TestBridgeAndSwapper:
    """The L1 BridgedNVNM token and the GuardedSwapper buyback-market wrapper."""

    async def test_bridged_nvnm_only_bridge_mints_and_burns(self, w3, chain_id, funded_account):
        """The L1 NVNM: supply moves only through a Safe-curated BRIDGE-role adapter."""
        owner = funded_account
        token = await _create(w3, chain_id, owner, BRIDGED_NVNM_BYTECODE + encode(["address"], [owner.address]).hex())

        async def send(signer, fn, expect=1):
            return await _send(w3, chain_id, signer, token, fn, expect)

        async def balance_of(who):
            return await ERC20.fns.balanceOf(who).call(w3, to=token)

        stranger, bridge = new_account(), new_account()
        await fund(w3, stranger.address)
        await fund(w3, bridge.address)
        await send(stranger, BRIDGED_NVNM.fns.bridgeMint(stranger.address, ETHER), expect=0)  # not a bridge

        await send(owner, BRIDGED_NVNM.fns.setRole(bridge.address, 1, True))  # owner grants BRIDGE
        await send(bridge, BRIDGED_NVNM.fns.bridgeMint(bridge.address, 100 * ETHER))
        assert await balance_of(bridge.address) == 100 * ETHER
        await send(bridge, BRIDGED_NVNM.fns.bridgeBurn(bridge.address, 40 * ETHER))
        assert await balance_of(bridge.address) == 60 * ETHER
        assert await ERC20.fns.totalSupply().call(w3, to=token) == 60 * ETHER  # burn shrank supply

    async def test_guarded_swapper_caps_size_and_price_floor(self, w3, chain_id, funded_account):
        """The buyback swapper caps swap size and rejects execution below its EMA price floor."""
        owner = funded_account

        async def send(to, fn, expect=1):
            return await _send(w3, chain_id, owner, to, fn, expect)

        async def swap(amount, expect=1):
            return await send(guard, GUARDED_SWAPPER.fns.swap(usd, nvnm, amount, 0), expect)

        usd = await _mock_token(w3, chain_id, owner, "USD")
        nvnm = await _mock_token(w3, chain_id, owner, "NVNM")

        # Seed a 1:1 pool and a guard (cap 50, -3% floor, EMA alpha 20%), reference price 1.0.
        pool = await _create(w3, chain_id, owner, POOL_BYTECODE + encode(["address", "address"], [usd, nvnm]).hex())
        for tok in (usd, nvnm):
            await send(tok, MOCK_ERC20.fns.mint(owner.address, 10_000 * ETHER))
            await send(tok, ERC20.fns.transfer(pool, 1_000 * ETHER))
        guard_arg = encode(["address", "address", "address"], [owner.address, usd, nvnm]).hex()
        guard = await _create(w3, chain_id, owner, GUARDED_SWAPPER_BYTECODE + guard_arg)
        await send(guard, GUARDED_SWAPPER.fns.setGuards(pool, 50 * ETHER, 300, 2_000))
        # The EMA may drift, but never more than 10% below the seeded reference: that band is
        # what stops the floor being walked down a swap at a time.
        await send(guard, GUARDED_SWAPPER.fns.setDriftBand(1_000))
        await send(guard, GUARDED_SWAPPER.fns.seedPrice(ETHER))
        await send(usd, ERC20.fns.approve(guard, 10_000 * ETHER))

        await swap(10 * ETHER)  # clears; drifts the EMA down
        assert await GUARDED_SWAPPER.fns.emaPrice().call(w3, to=guard) < ETHER
        await swap(40 * ETHER, expect=0)  # below the price floor
        # Over the size cap must revert on the cap specifically — the cap is checked before the
        # swap, so a regression removing it would surface as a PriceBelowFloor revert instead.
        # `sender` matters: the swap gate runs first, so an unset from-address would revert with
        # NotAuthorized and mask whichever guard this is actually asserting.
        err = await call_revert(
            w3, guard, GUARDED_SWAPPER.fns.swap(usd, nvnm, 51 * ETHER, 0).data, sender=owner.address
        )
        assert ERR_AMOUNT_TOO_LARGE in err, f"expected AmountTooLarge, got {err}"

        # With no router factory configured, the owner is the only caller: an open `swap` is
        # near-free price manipulation, since a direct caller keeps the output.
        stranger = new_account()
        await fund(w3, stranger.address)
        await _send(w3, chain_id, stranger, guard, GUARDED_SWAPPER.fns.swap(usd, nvnm, ETHER, 0), expect=0)


# -- native epoch feed (opt-in: --consensus + a feed-capable binary) -----------------------------
# Genesis carries the precomputed proxy address; the node's feed eth_calls computeCommittee() at a
# block hash from address(0). A binary without the feed keeps all validators and fails these.

LOCALNET_CHAIN_ID = 1337  # the election localnet's genesis chain id
N_ELECTION_VALIDATORS = 5  # elect 4 of 5: passes the node's min(4, registry) floor
EPOCH_LENGTH = 20  # blocks; see the fixture — these tests are paced by epoch boundaries


# Dedicated key, kept fresh so its nonce-0 create is StakingDeployer at a known address.
ELECTION_DEPLOYER = Account.from_key(keccak(b"nvm-election-deployer"))


def _staking_genesis_address() -> str:
    return _create_address(_create_address(ELECTION_DEPLOYER.address, 0), _STAKING_PROXY_NONCE)


async def _traffic_until_fees(w3, faucet, recipients, tries=20):
    """Send cheap txs until one of `recipients` has collected, and return (it, its balance).

    Fees only accrue when a proposer whose fee recipient is in the set actually builds a block,
    so this polls rather than sending one tx.
    """
    for _ in range(tries):
        await _send(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, ERC20.fns.transfer(faucet.address, 1))
        for recipient in recipients:
            if collected := await FEE.fns.collectedFees(recipient, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS):
                return recipient, collected
    pytest.fail(f"no chain fees accrued to any of {recipients}")


async def _committee(w3):
    """The live committee as (epoch, {pubkey bytes})."""
    epoch, keys = await CURRENT_COMMITTEE.fns.getCommitteeMembers().call(w3, to=CURRENT_COMMITTEE_ADDRESS)
    return epoch, {bytes(k) for k in keys}


async def _wait_committee(w3, predicate, why, timeout=360):
    """Poll the committee until ``predicate(epoch, members)`` holds; fail with context on timeout."""
    deadline = time.time() + timeout
    epoch, members = -1, set()
    while time.time() < deadline:
        epoch, members = await _committee(w3)
        if predicate(epoch, members):
            return epoch, members
        await asyncio.sleep(3)
    pytest.fail(f"{why} (block {await w3.eth.block_number}, epoch {epoch}, {len(members)} members)")


@pytest.fixture(scope="module")
def election_net(request, tmp_path_factory):
    """A 5-validator supervisord localnet whose genesis carries `stakingElection`."""
    if not request.config.getoption("--consensus"):
        pytest.skip("staking-election feed test needs --consensus")
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("staking-election")
    config = {
        "chain_id": LOCALNET_CHAIN_ID,
        "accounts": 200,
        # These tests are paced by epoch boundaries — the election is read at each one, and
        # `test_committee_follows_staking_election` waits for three. At the usual 100 that is
        # ~4 minutes of pure waiting on a localnet producing ~1.5 blocks/s. Short enough to be
        # quick, long enough for the boundary DKG to finish before the next one opens.
        "epoch_length": EPOCH_LENGTH,
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


@pytest.fixture
async def election_w3(election_net):
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(election_net.node_rpc_url("node0")))
    # Genesis extraData holds the validator set and epoch-boundary headers the DKG outcome,
    # both past web3.py's 32-byte cap. Without this, passing depends on which block is touched.
    client.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    yield client
    await client.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
class TestEpochFeed:
    """The node-side staking-election feed driving the consensus committee."""

    async def test_distribute_fees_pays_the_collected_amount(self, election_w3):
        """Tx fees accrue under block proposers' fee recipients; distributeFees pays them out."""
        w3 = election_w3
        faucet = Account.from_key(FAUCET_PRIVATE_KEY)
        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
        recipients = [cs(v[4]) for v in active if int(v[4], 16) != 0]
        assert recipients, "localnet validators have no fee recipients"

        pick, collected = await _traffic_until_fees(w3, faucet, recipients)

        before = await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD)
        await _send(w3, LOCALNET_CHAIN_ID, faucet, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(pick, PATH_USD))
        # More fees may accrue for `pick` between the read and the payout, so >=.
        assert await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD) - before >= collected

    async def test_committee_follows_staking_election(self, election_w3):
        """Below min(4, registry) the node keeps the full registry (fallback); at/above it, the
        committee shrinks to exactly the elected set."""
        w3 = election_w3
        deployer, faucet = ELECTION_DEPLOYER, Account.from_key(FAUCET_PRIVATE_KEY)
        # Fund fresh deployer, its nonce-0 create must land on the genesis-patched address.
        await _send(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, ERC20.fns.transfer(deployer.address, 10**14))
        assert await w3.eth.get_transaction_count(deployer.address) == 0, "election deployer must be fresh"
        init = DEPLOYER_BYTECODE + encode(["address"], [PATH_USD]).hex()
        d = await _create(w3, LOCALNET_CHAIN_ID, deployer, init)
        staking_addr = cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d))
        assert staking_addr == _staking_genesis_address(), "proxy != genesis stakingElection address"
        nvnm = cs(await STAKING_DEPLOYER.fns.nvnm().call(w3, to=d))
        staking = Staking(w3, LOCALNET_CHAIN_ID, staking_addr, nvnm, PATH_USD)

        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
        assert len(active) == N_ELECTION_VALIDATORS
        key_by_addr = {cs(v[1]): bytes(v[0]) for v in active}
        ranked = sorted(key_by_addr)  # deterministic order by address
        await staking.send(deployer, STAKING.fns.setCommitteeConfig(21, 1, 0))

        async def elect(addr):
            await staking.send(deployer, STAKING.fns.setCandidate(addr, True))
            await staking.stake(deployer, addr, 100 * ETHER)

        # Phase 1 — elect only 3 of 5: below the min(4, registry) floor, so the node must keep the
        # full registry. Let two epoch boundaries pass (the election is read at each), then confirm
        # the committee still holds all 5 — i.e. the read fell back rather than shrinking.
        for addr in ranked[:3]:
            await elect(addr)
        start_epoch, _ = await _committee(w3)
        _, members = await _wait_committee(
            w3,
            lambda e, _m: e >= start_epoch + 2,
            "epoch did not advance to confirm the below-minimum election was read",
        )
        assert members == set(key_by_addr.values()), (
            f"below-minimum election shrank committee to {len(members)}, expected fallback"
        )

        # Phase 2 — add the 4th: now at the floor, the committee shrinks to exactly those 4.
        await elect(ranked[3])
        expected = {key_by_addr[a] for a in ranked[:4]}
        await _wait_committee(w3, lambda _e, m: m == expected, "committee never matched the 4 elected")
