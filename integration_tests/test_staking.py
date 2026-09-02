"""Delegated staking that shares chain fees with stakers, over JSON-RPC.

Against the dev node: core staking, committee election, unbonding and slashing, fee
routing -- including the shared pool NVM plans to run -- and the bridge and swapper. The
stack itself is stood up by ``staking.py``; the node-side epoch feed, which needs a
consensus localnet, is ``test_epoch_feed.py``.
"""

import asyncio

import pytest
from eth_abi.abi import encode
from eth_contract.erc20 import ERC20
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD

from .abi import (
    BRIDGED_NVNM,
    FEE,
    FEE_ROUTER,
    GUARDED_SWAPPER,
    MOCK_ERC20,
    STAKING,
)
from .staking import (
    BRIDGED_NVNM_BYTECODE,
    ERR_AMOUNT_TOO_LARGE,
    ETHER,
    GLOBAL_POOL,
    GUARDED_SWAPPER_BYTECODE,
    POOL_BYTECODE,
    create,
    cs,
    deploy,
    deploy_stack,
    mock_token,
    router_setup,
    transact,
)
from .utils import call_revert, fund, gas_cost_in_token, new_account

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD


@pytest.fixture
async def staking(w3, chain_id, funded_account):
    return await deploy(w3, chain_id, funded_account)


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
        await staking.setup_election(funded_account, [v1, v2])

        await staking.stake(funded_account, v1, 300 * ETHER)
        await staking.stake(funded_account, v2, 150 * ETHER)

        assert await staking.elected() == [v1, v2]

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
        await staking.setup_election(funded_account, [v1, v2])
        await staking.stake(funded_account, v1, 300 * ETHER)
        snapshot = await staking.w3.eth.block_number

        await staking.stake(funded_account, v2, 100 * ETHER)  # committee changes after `snapshot`

        assert await staking.elected(block_identifier=snapshot) == [v1]
        assert await staking.elected() == [v1, v2]

    async def test_read_from_system_caller(self, staking, funded_account):
        """From address(0), an under-staked committee decodes as empty (the fallback trigger)."""
        zero = "0x" + "00" * 20
        v = new_account().address
        await staking.setup_election(funded_account, [v])

        assert await staking.elected(**{"from": zero}) == []  # no qualifying stake yet

        await staking.stake(funded_account, v, 200 * ETHER)
        assert await staking.elected(**{"from": zero}) == [v]

    async def test_read_fits_node_gas_cap(self, staking, funded_account):
        """The election read fits the node's call cap, measured by the node itself.

        The unit suite bounds the worst case (a full candidate list); this checks the real
        estimate a node produces, which is what the consensus feed is actually billed.
        """
        validators = [new_account().address for _ in range(5)]
        await staking.setup_election(funded_account, validators)
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
        staking = await deploy(w3, chain_id, funded_account, reward_token=PATH_USD)
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
        staking, val, operator, router, _, _ = await router_setup(w3, chain_id, funded_account, 1_000)
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
        setup = await router_setup(w3, chain_id, funded_account, 10_000, split=True)
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
        setup = await router_setup(w3, chain_id, funded_account, 1_000, split=True)
        staking, val, operator, router, treasury, buybacks = setup
        await staking.stake(funded_account, val, 100 * ETHER)

        other = await mock_token(w3, chain_id, funded_account, "otherUSD")
        fees = 100 * ETHER
        await transact(w3, chain_id, funded_account, other, MOCK_ERC20.fns.mint(router, fees))
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
        await transact(w3, chain_id, funded_account, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(recipient, PATH_USD))

    async def test_all_validators_fees_reach_one_staker_pool(self, w3, chain_id, funded_account):
        """Two validators, two routers, one pool: a staker who picked no operator earns from both."""
        owner = funded_account
        # The same stack the localnet deploys, minus a registry to repoint and the protocol
        # cuts: two routers keyed to the shared pool, each paying its own operator.
        staking, routers, payouts = await deploy_stack(
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
        token = await create(w3, chain_id, owner, BRIDGED_NVNM_BYTECODE + encode(["address"], [owner.address]).hex())

        async def send(signer, fn, expect=1):
            return await transact(w3, chain_id, signer, token, fn, expect)

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
            return await transact(w3, chain_id, owner, to, fn, expect)

        async def swap(amount, expect=1):
            return await send(guard, GUARDED_SWAPPER.fns.swap(usd, nvnm, amount, 0), expect)

        usd = await mock_token(w3, chain_id, owner, "USD")
        nvnm = await mock_token(w3, chain_id, owner, "NVNM")

        # Seed a 1:1 pool and a guard (cap 50, -3% floor, EMA alpha 20%), reference price 1.0.
        pool = await create(w3, chain_id, owner, POOL_BYTECODE + encode(["address", "address"], [usd, nvnm]).hex())
        for tok in (usd, nvnm):
            await send(tok, MOCK_ERC20.fns.mint(owner.address, 10_000 * ETHER))
            await send(tok, ERC20.fns.transfer(pool, 1_000 * ETHER))
        guard_arg = encode(["address", "address", "address"], [owner.address, usd, nvnm]).hex()
        guard = await create(w3, chain_id, owner, GUARDED_SWAPPER_BYTECODE + guard_arg)
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
        await transact(w3, chain_id, stranger, guard, GUARDED_SWAPPER.fns.swap(usd, nvnm, ETHER, 0), expect=0)
