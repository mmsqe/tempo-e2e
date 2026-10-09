"""The staking stack on a dev node: staking, election, unbonding, slashing, fee routing, and the
bridged token and swapper. The node's epoch feed is in ``test_epoch_feed.py``."""

import pytest
from eth_abi.abi import encode
from eth_account import Account
from eth_contract.create3 import create3_address
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD
from tempo.constants import VALIDATOR_CONFIG_V2_ADDRESS as V2_ADDR

from .abi import (
    BRIDGED_NVNM,
    FEE,
    FEE_LOCKBOX,
    FEE_ROUTER,
    FEE_ROUTER_FACTORY,
    GUARDED_SWAPPER,
    STAKING,
)
from .abi import VALIDATOR_CONFIG_V2 as V2
from .network import FAUCET_PRIVATE_KEY
from .staking import (
    BRIDGED_NVNM_BYTECODE,
    DAY,
    ETHER,
    GUARDED_SWAPPER_BYTECODE,
    POOL_BYTECODE,
    SPLIT_BPS,
    SPLIT_DELAY,
    create3,
    create3_call,
    create3_salt,
    created_routers,
    deploy,
    deploy_stack,
    factory_initcode,
    fee_router,
    lockbox_initcode,
    router_setup,
    tip20,
)
from .test_validator_config import join
from .utils import (
    call_revert,
    cs,
    funded,
    gas_cost_in_token,
    latest_timestamp,
    new_account,
    rejects,
    send_call,
    wait_for_timestamp,
)

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD


@pytest.fixture
async def staking(w3, chain_id, funded_account):
    return await deploy(w3, chain_id, funded_account)


ZERO = "0x" + "00" * 20
SINK = "0x000000000000000000000000000000000000dEaD"  # FeeRouterFactory.BUYBACK_SINK
# Each buyback swap's gas: a guard's first swap writes several fresh TIP-20 balance slots.
SWAP_GAS = 3_000_000


@pytest.fixture
async def voters(w3, chain_id):
    """`make(n)`: n new validators in the node's registry, each an account with gas. They
    deactivate themselves afterwards, so the active set other tests read is left as it was."""
    owner, joined = Account.from_key(FAUCET_PRIVATE_KEY), []

    async def make(n):
        made = []
        for _ in range(n):
            v = await funded(w3)
            count = await V2.fns.validatorCount().call(w3, to=V2_ADDR)
            _, receipt = await join(
                w3, chain_id, owner, v.address, f"10.20.{count // 250 % 250}.{1 + count % 250}:26656"
            )
            assert receipt["status"] == 1
            joined.append(v)
            made.append(v)
        return made

    yield make
    for v in joined:
        index = (await V2.fns.validatorByAddress(v.address).call(w3, to=V2_ADDR))[5]
        await send_call(w3, chain_id, v, V2_ADDR, V2.fns.deactivateValidator(index).data)


async def commence(w3, staking, owner, lockbox, voters):
    """Commence `lockbox`: the live set declared independent, and outvoted by fresh validators."""
    active = [v[1] for v in await V2.fns.getActiveValidators().call(w3, to=V2_ADDR)]
    fresh = await voters(len(active) + 1)
    for v in active + [v.address for v in fresh]:
        await staking.send(owner, FEE_LOCKBOX.fns.setAffiliated(v, False), to=lockbox)
    for v in fresh:
        await staking.send(v, FEE_LOCKBOX.fns.vote(True), to=lockbox)
    await staking.send(owner, FEE_LOCKBOX.fns.commence(), to=lockbox)


class TestStaking:
    """Core delegation: stake, pro-rata reward share, unstake, compounding."""

    async def test_stake_deposit_claim(self, staking, funded_account):
        me, val = funded_account.address, new_account().address
        await staking.stake(funded_account, val, 100 * ETHER)
        assert await staking.staked_of(val, me) == 100 * ETHER

        await staking.deposit_reward(funded_account, val, 500 * ETHER)
        await staking.vest(val)
        assert await staking.earned(val, me) == 500 * ETHER

        before = await staking.balance(staking.usd, me)
        await staking.send(funded_account, STAKING.fns.claim(val))
        assert await staking.balance(staking.usd, me) - before == 500 * ETHER
        assert await staking.earned(val, me) == 0

    async def test_rewards_split_pro_rata(self, staking, funded_account):
        val, bob = new_account().address, await funded(staking.w3)
        await staking.transfer(funded_account, staking.nvnm, bob.address, 100 * ETHER)

        await staking.stake(funded_account, val, 300 * ETHER)
        await staking.stake(bob, val, 100 * ETHER)  # 3:1
        await staking.deposit_reward(funded_account, val, 400 * ETHER)
        await staking.vest(val)

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

    async def test_unstaking_more_than_staked_reverts(self, staking, funded_account):
        v = new_account().address
        await staking.stake(funded_account, v, 10 * ETHER)
        unstake = STAKING.fns.unstake(v, 11 * ETHER)
        await rejects(staking.w3, staking.address, unstake, "InsufficientStake", sender=funded_account.address)

    async def test_a_reward_with_no_stakers_reverts(self, staking, funded_account):
        """A direct deposit into an empty pool has nowhere to go; the router pays the operator."""
        deposit = STAKING.fns.depositReward(new_account().address, ETHER)
        await rejects(staking.w3, staking.address, deposit, "NoStakers", sender=funded_account.address)

    async def test_zero_amounts_revert(self, staking, funded_account):
        v = new_account().address
        for fn in (STAKING.fns.stake(v, 0), STAKING.fns.unstake(v, 0), STAKING.fns.depositReward(v, 0)):
            await rejects(staking.w3, staking.address, fn, "ZeroAmount", sender=funded_account.address)


class TestCommitteeElection:
    """Top-N equal-seat election and the deterministic read the node's feed relies on."""

    async def test_ranks_by_stake_one_seat_each(self, staking, funded_account):
        """Committee is top-N, one equal seat (engine is unit-weighted)."""
        v1, v2 = new_account().address, new_account().address
        await staking.setup_election(funded_account, [v1, v2])

        await staking.stake(funded_account, v1, 300 * ETHER)
        await staking.stake(funded_account, v2, 150 * ETHER)

        assert await staking.elected() == [v1, v2]

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
        v = new_account().address
        await staking.setup_election(funded_account, [v])

        assert await staking.elected(**{"from": ZERO}) == []  # no qualifying stake yet

        await staking.stake(funded_account, v, 200 * ETHER)
        assert await staking.elected(**{"from": ZERO}) == [v]

    async def test_read_fits_node_gas_cap(self, staking, funded_account):
        """The election read fits the node's call cap, as the node itself estimates it."""
        validators = [new_account().address for _ in range(5)]
        await staking.setup_election(funded_account, validators)
        for v in validators:
            await staking.stake(funded_account, v, 100 * ETHER)

        read = STAKING.fns.computeCommittee(validators)
        gas = await staking.w3.eth.estimate_gas({"from": ZERO, "to": staking.address, "data": read.data})
        assert gas < 30_000_000, f"election read too expensive for the node cap: {gas}"

    async def test_below_min_seats_elects_nobody(self, staking, funded_account):
        """Fewer qualifying candidates than `minSeats` elects nobody; the node falls back."""
        v1, v2 = new_account().address, new_account().address
        await staking.setup_election(funded_account, [v1, v2])
        for v in (v1, v2):
            await staking.stake(funded_account, v, 100 * ETHER)

        await staking.send(funded_account, STAKING.fns.setMinSeats(3))
        assert await staking.elected() == []
        await staking.send(funded_account, STAKING.fns.setMinSeats(2))
        assert sorted(await staking.elected()) == sorted([v1, v2])

    async def test_drops_candidates_outside_the_registry(self, staking, funded_account):
        """The node passes its registry; a heavier candidate outside it cannot take the seat."""
        v1, v2 = new_account().address, new_account().address
        await staking.setup_election(funded_account, [v1, v2], seats=1)
        await staking.stake(funded_account, v1, 300 * ETHER)
        await staking.stake(funded_account, v2, 100 * ETHER)

        assert await staking.elected() == [v1]
        assert await staking.elected(eligible=[v2]) == [v2]

    async def test_stake_past_the_delegation_cap_reverts(self, staking, funded_account):
        v = new_account().address
        await staking.send(funded_account, STAKING.fns.setUnbondingPeriod(DAY))
        await staking.send(funded_account, STAKING.fns.setCommitteeConfig(21, 1, 50 * ETHER))
        stake = STAKING.fns.stake(v, 51 * ETHER)
        await rejects(staking.w3, staking.address, stake, "DelegationCap", sender=funded_account.address)

    async def test_the_committee_seats_at_most_21(self, staking, funded_account):
        over = STAKING.fns.setCommitteeConfig(22, 1, 0)
        await rejects(staking.w3, staking.address, over, "TooManySeats", sender=funded_account.address)

    async def test_candidacy_is_closed_without_a_bond(self, staking):
        stranger = new_account().address
        register = STAKING.fns.registerCandidate()
        await rejects(staking.w3, staking.address, register, "CandidacyClosed", sender=stranger)

    async def test_only_the_owner_configures_the_election(self, staking):
        stranger, v = new_account().address, new_account().address
        for fn in (
            STAKING.fns.setCandidate(v, True),
            STAKING.fns.setCommitteeConfig(21, 1, 0),
            STAKING.fns.setMinSeats(1),
            STAKING.fns.setCandidacyBond(ETHER),
            STAKING.fns.setRewardDuration(1),
        ):
            await rejects(staking.w3, staking.address, fn, "Unauthorized", sender=stranger)


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

        await wait_for_timestamp(staking.w3, release_at)
        await staking.send(funded_account, STAKING.fns.withdraw(val))
        assert await staking.balance(staking.nvnm, me) == before + 100 * ETHER
        assert (await staking.call(STAKING.fns.pendingUnstakeOf(val, me)))[0] == 0

    async def test_a_slash_never_reaches_delegators(self, staking, funded_account):
        """Only the bond is at risk, which a bond bridged in shows (test_bridge.py): every
        delegated token survives a full slash, live stake and the unbonding bucket alike."""
        me, val = funded_account.address, new_account().address
        await staking.setup_election(funded_account, [val], unbonding=2)  # slashing opens with it

        # A delegator holding stake both live and unbonding, so the slash has both to miss.
        await staking.stake(funded_account, val, 200 * ETHER)
        await staking.send(funded_account, STAKING.fns.unstake(val, 100 * ETHER))

        await staking.send(funded_account, STAKING.fns.setSlasher(me))
        await staking.send(funded_account, STAKING.fns.slash(val, 10_000))
        assert await staking.staked_of(val, me) == 100 * ETHER, "live stake untouched"
        amount, release_at = await staking.call(STAKING.fns.pendingUnstakeOf(val, me))
        assert amount == 100 * ETHER

        await wait_for_timestamp(staking.w3, release_at)
        before = await staking.balance(staking.nvnm, me)
        await staking.send(funded_account, STAKING.fns.withdraw(val))
        assert await staking.balance(staking.nvnm, me) - before == 100 * ETHER

    async def test_withdrawing_early_or_with_nothing_pending_reverts(self, staking, funded_account):
        v, me = new_account().address, funded_account.address
        await staking.send(funded_account, STAKING.fns.setUnbondingPeriod(DAY))
        await staking.stake(funded_account, v, 10 * ETHER)
        await staking.send(funded_account, STAKING.fns.unstake(v, 10 * ETHER))

        withdraw = STAKING.fns.withdraw(v)
        await rejects(staking.w3, staking.address, withdraw, "StillUnbonding", sender=me)
        await rejects(staking.w3, staking.address, withdraw, "NothingToWithdraw", sender=new_account().address)

    async def test_only_the_slasher_slashes(self, staking, funded_account):
        """Only the slasher slashes, not the owner or address(0), and only once the election is configured."""
        v = new_account().address
        slash = STAKING.fns.slash(v, 1_000)
        for caller in (new_account().address, ZERO, funded_account.address):
            await rejects(staking.w3, staking.address, slash, "NotSlasher", sender=caller)
        await staking.send(funded_account, STAKING.fns.setSlasher(funded_account.address))
        too_much = STAKING.fns.slash(v, 10_001)
        await rejects(staking.w3, staking.address, too_much, "InvalidBps", sender=funded_account.address)
        await rejects(staking.w3, staking.address, slash, "SlashingClosed", sender=funded_account.address)


class TestFeeRouting:
    """FeeRouter: protocol cuts then validator remainder → commission + delegators."""

    async def test_rewards_paid_in_real_fee_stablecoin(self, w3, chain_id, funded_account):
        """The production reward leg: the contract pulls and pays out PATH_USD (TIP-20 precompile)."""
        staking = await deploy(w3, chain_id, funded_account, reward_token=PATH_USD)
        assert cs(await staking.call(STAKING.fns.rewardToken())) == cs(PATH_USD)

        me, val = funded_account.address, new_account().address
        await staking.stake(funded_account, val, 100 * ETHER)

        reward = 400 * 10**6  # PATH_USD base units (faucet funds far more)
        await staking.deposit_reward(funded_account, val, reward)
        await staking.vest(val)
        assert await staking.earned(val, me) == reward

        before = await staking.balance(PATH_USD, me)
        receipt = await staking.send(funded_account, STAKING.fns.claim(val))
        # The claim tx's own gas is also paid in PATH_USD, so net it out of the delta.
        assert await staking.balance(PATH_USD, me) - before == reward - gas_cost_in_token(receipt)

    async def test_router_splits_fees_to_operator_and_stakers(self, w3, chain_id, funded_account, voters):
        """Before commencement the lockbox holds the remainder for the operator; after it, the commission
        is paid directly and the rest staked."""
        staking, val, operator, router, _, lockbox = await router_setup(w3, chain_id, funded_account, 1_000)
        me = funded_account.address
        await staking.stake(funded_account, val, 100 * ETHER)
        keeper = await funded(w3)

        # Fees arrive on the router (FeeManager's distributeFees payout is exactly this transfer).
        fees = 200 * 10**6
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(keeper, FEE_ROUTER.fns.flush(), to=router)
        assert await FEE_LOCKBOX.fns.owed(PATH_USD, operator).call(w3, to=lockbox) == fees // 2
        assert await staking.earned(val, me) == 0, "the pool waits for commencement too"

        await commence(w3, staking, funded_account, lockbox, voters)
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(keeper, FEE_ROUTER.fns.flush(), to=router)
        commission = fees // 20  # 10% of the half the cuts leave
        assert await staking.balance(PATH_USD, operator) == commission
        await staking.vest(val)
        assert await staking.earned(val, me) == fees // 2 - commission
        assert await staking.balance(PATH_USD, router) == 2 * (fees // 4), "only the held buyback cuts"

    async def test_protocol_split_pays_devshare_and_buyback(self, w3, chain_id, funded_account):
        """25/25 protocol cuts, the buyback cut held with no swapper set; the validator remainder is
        the operator's when the pool is empty."""
        staking, val, operator, router, treasury, lockbox = await router_setup(w3, chain_id, funded_account, 10_000)

        fees = 200 * 10**6
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(funded_account, FEE_ROUTER.fns.flush(), to=router)

        assert await staking.balance(PATH_USD, treasury) == fees // 4
        assert await staking.call(FEE_ROUTER.fns.heldForBuyback(PATH_USD), to=router) == fees // 4
        assert await FEE_LOCKBOX.fns.owed(PATH_USD, operator).call(w3, to=lockbox) == fees // 2
        assert await staking.earned(val, funded_account.address) == 0

    async def test_flush_waterfalls_a_second_fee_token(self, w3, chain_id, funded_account, voters):
        """A non-reward token still takes the cuts; once commenced, its delegator share is escrowed, even
        across a second flush."""
        staking, val, operator, router, treasury, lockbox = await router_setup(w3, chain_id, funded_account, 1_000)
        await staking.stake(funded_account, val, 100 * ETHER)
        await commence(w3, staking, funded_account, lockbox, voters)

        fees = 100 * ETHER
        other = await tip20(w3, chain_id, funded_account, "otherUSD", amount=fees)
        await staking.transfer(funded_account, other, router, fees)
        await staking.send(funded_account, FEE_ROUTER.fns.flush(other), to=router)

        held = fees // 2 - fees // 20  # the validator remainder less the 10% commission
        assert await staking.balance(other, treasury) == fees // 4
        assert await staking.call(FEE_ROUTER.fns.heldForBuyback(other), to=router) == fees // 4
        assert await staking.balance(other, operator) == fees // 20
        assert await staking.balance(other, router) == held + fees // 4
        assert await staking.call(FEE_ROUTER.fns.heldForDelegators(other), to=router) == held
        assert await staking.earned(val, funded_account.address) == 0

        keeper = await funded(w3)
        await staking.send(keeper, FEE_ROUTER.fns.flush(other), to=router)
        assert await staking.balance(other, treasury) == fees // 4, "devshare not taken twice"
        assert await staking.balance(other, router) == held + fees // 4, "held shares intact"

    async def test_a_router_is_paid_in_the_reward_token_from_creation(self, w3, chain_id, funded_account):
        """Left alone, FeeManager pays a router the chain's default token, which a pool rewarding in
        another (USDT0 on mainnet) can neither deposit nor swap."""
        owner = funded_account
        staking = await deploy(w3, chain_id, owner)  # rewards in a fresh USD TIP-20
        _, router, _ = await fee_router(
            staking, owner, new_account().address, new_account().address, 10_000, new_account().address
        )
        assert cs(await FEE.fns.validatorTokens(router).call(w3, to=FEE_MANAGER_ADDRESS)) == staking.usd

    async def test_distribute_fees_is_permissionless(self, w3, chain_id, funded_account):
        """Anyone may trigger the FeeManager's payout for any fee recipient (a router included)."""
        recipient = new_account().address  # nothing collected: succeeds as a no-op, no auth gate
        distribute = FEE.fns.distributeFees(recipient, PATH_USD)
        await send_call(w3, chain_id, funded_account, FEE_MANAGER_ADDRESS, distribute.data)

    async def test_each_validators_fees_reach_only_its_own_pool(self, w3, chain_id, funded_account, voters):
        """Two validators, two routers, two pools: once distribution has commenced, each router's
        delegator share lands in its own validator's pool."""
        owner = funded_account
        # The same stack the localnet deploys, minus a registry to repoint.
        staking, routers, pools, payouts, lockbox = await deploy_stack(
            w3, chain_id, owner, routers=2, commission_bps=1_000, stake=100 * ETHER, treasury=new_account().address
        )
        await commence(w3, staking, owner, lockbox, voters)

        fees = [200 * 10**6, 600 * 10**6]
        for router, fee in zip(routers, fees, strict=True):
            await staking.transfer(owner, PATH_USD, router, fee)
            await staking.send(owner, FEE_ROUTER.fns.flush(), to=router)

        for pool, payout, fee in zip(pools, payouts, fees, strict=True):
            await staking.vest(pool)
            assert await staking.earned(pool, owner.address) == fee // 2 - fee // 20
            assert await staking.balance(PATH_USD, payout) == fee // 20

    async def test_the_factory_bounds_commission_and_reads_the_lockbox_split(self, w3, chain_id, funded_account):
        staking = await deploy(w3, chain_id, funded_account, reward_token=PATH_USD)
        me = funded_account.address
        treasury = new_account().address
        lockbox = await create3(w3, chain_id, funded_account, lockbox_initcode(me))
        initcode = factory_initcode(staking.address, lockbox, me, treasury, max_commission=5_000)
        factory = await create3(w3, chain_id, funded_account, initcode)

        greedy = FEE_ROUTER_FACTORY.fns.create(new_account().address, new_account().address, 5_001)
        await rejects(w3, factory, greedy, "CommissionTooHigh", sender=me)
        dev, buy, dev_bps, buy_bps, _, _ = await FEE_ROUTER_FACTORY.fns.cuts().call(w3, to=factory)
        assert (cs(dev), cs(buy), (dev_bps, buy_bps)) == (treasury, SINK, SPLIT_BPS)

    async def test_only_a_validator_or_the_owner_creates_its_router(self, w3, chain_id, funded_account):
        """The registry takes whichever router the factory last made for a validator, and a router
        names who is paid the operator share."""
        staking = await deploy(w3, chain_id, funded_account, reward_token=PATH_USD)
        validator, stranger = await funded(w3), new_account().address
        factory, _, _ = await fee_router(
            staking, funded_account, validator.address, new_account().address, 1_000, new_account().address
        )
        create = FEE_ROUTER_FACTORY.fns.create(validator.address, stranger, 2_000)
        await rejects(w3, factory, create, "Unauthorized", sender=stranger)
        assert created_routers(await staking.send(validator, create, to=factory))

    async def test_a_create3_address_follows_its_deployer_and_salt_alone(self, w3, chain_id, funded_account):
        """What lets a genesis name a contract before its constructor arguments are known."""
        me = funded_account.address
        salt = create3_salt(me)
        mine = create3_address(salt, deployer=me)

        # Another sender is not refused the salt: it lands where an unguarded salt does.
        await create3(w3, chain_id, await funded(w3), lockbox_initcode(me), salt)
        assert await w3.eth.get_code(create3_address(salt))
        assert not await w3.eth.get_code(mine)

        await create3(w3, chain_id, funded_account, lockbox_initcode(me), salt)
        assert await w3.eth.get_code(mine)

        # Taken: other arguments under the same salt cannot replace what is there.
        again = create3_call(salt, lockbox_initcode(new_account().address))
        failed = keccak(text="FailedContractCreation(address)")[:4].hex()
        assert failed in await call_revert(w3, **again, sender=me)

    async def test_a_lockbox_starts_on_a_split_with_buybacks_at_20_percent_or_more(self, w3, chain_id, funded_account):
        initcode = lockbox_initcode(funded_account.address, split=(3_000, 1_999))
        with pytest.raises(AssertionError, match=keccak(text="BuybackBelowFloor()")[:4].hex()):
            await create3(w3, chain_id, funded_account, initcode)


class TestFeeLockbox:
    """FeeLockbox against the node's own registry: commission waits for commencement, and the
    active set votes the split."""

    async def test_commission_waits_for_a_non_affiliated_majority_vote(self, w3, chain_id, funded_account, voters):
        """An affiliated majority cannot commence; declared non-affiliated voters can, which releases the
        held commission."""
        staking, _, operator, router, _, lockbox = await router_setup(w3, chain_id, funded_account, 10_000)
        me, fees = funded_account.address, 200 * 10**6
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(funded_account, FEE_ROUTER.fns.flush(), to=router)
        held = fees // 2  # the remainder after the cuts, all commission with no pool
        assert await FEE_LOCKBOX.fns.owed(PATH_USD, operator).call(w3, to=lockbox) == held
        await rejects(w3, lockbox, FEE_LOCKBOX.fns.claim(PATH_USD, operator), "NotCommenced", sender=me)

        # Validators other tests left active count too: declared independent, they never vote.
        others = [v[1] for v in await V2.fns.getActiveValidators().call(w3, to=V2_ADDR)]
        for v in others:
            await staking.send(funded_account, FEE_LOCKBOX.fns.setAffiliated(v, False), to=lockbox)
        for v in await voters(len(others) + 1):
            await staking.send(funded_account, FEE_LOCKBOX.fns.setAffiliated(v.address, True), to=lockbox)
            await staking.send(v, FEE_LOCKBOX.fns.vote(True), to=lockbox)
        await rejects(w3, lockbox, FEE_LOCKBOX.fns.commence(), "MajorityAffiliated", sender=me)

        late = await voters(len(others) + 2)
        for v in late:
            await staking.send(v, FEE_LOCKBOX.fns.vote(True), to=lockbox)
        await rejects(w3, lockbox, FEE_LOCKBOX.fns.commence(), "Undeclared", sender=me)
        for v in late:
            await staking.send(funded_account, FEE_LOCKBOX.fns.setAffiliated(v.address, False), to=lockbox)
        await staking.send(funded_account, FEE_LOCKBOX.fns.commence(), to=lockbox)

        await staking.send(funded_account, FEE_LOCKBOX.fns.claim(PATH_USD, operator), to=lockbox)
        assert await staking.balance(PATH_USD, operator) == held
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(funded_account, FEE_ROUTER.fns.flush(), to=router)
        assert await staking.balance(PATH_USD, operator) == 2 * held, "paid directly once commenced"

    async def test_the_set_votes_a_split_in_after_its_delay(self, w3, chain_id, funded_account, voters):
        """A proposal applies once a majority of the live set has backed it for the delay; before
        commencement it may not raise devshare. The next flush cuts by the applied split."""
        staking, _, operator, router, treasury, lockbox = await router_setup(w3, chain_id, funded_account, 10_000)
        me = funded_account.address
        others = len(await V2.fns.getActiveValidators().call(w3, to=V2_ADDR))
        # The proposer alone is never a majority, the proposer and backers always are.
        proposer, *backers = await voters(others + 2)

        await staking.send(proposer, FEE_LOCKBOX.fns.proposeSplit(3_000, 2_000), to=lockbox)  # id 0
        await staking.send(proposer, FEE_LOCKBOX.fns.proposeSplit(1_500, 3_000), to=lockbox)  # id 1
        await rejects(w3, lockbox, FEE_LOCKBOX.fns.applySplit(1), "VoteShort", sender=me)

        for v in backers:
            await staking.send(v, FEE_LOCKBOX.fns.voteSplit(0, True), to=lockbox)
            await staking.send(v, FEE_LOCKBOX.fns.voteSplit(1, True), to=lockbox)
        await rejects(w3, lockbox, FEE_LOCKBOX.fns.applySplit(1), "SplitPending", sender=me)
        await wait_for_timestamp(w3, await latest_timestamp(w3) + SPLIT_DELAY)
        await rejects(w3, lockbox, FEE_LOCKBOX.fns.applySplit(0), "DevshareRaised", sender=me)
        await staking.send(funded_account, FEE_LOCKBOX.fns.applySplit(1), to=lockbox)
        assert await FEE_LOCKBOX.fns.split().call(w3, to=lockbox) == (1_500, 3_000)

        fees = 200 * 10**6
        await staking.transfer(funded_account, PATH_USD, router, fees)
        await staking.send(funded_account, FEE_ROUTER.fns.flush(), to=router)
        assert await staking.balance(PATH_USD, treasury) == fees * 15 // 100
        assert await staking.call(FEE_ROUTER.fns.heldForBuyback(PATH_USD), to=router) == fees * 30 // 100
        assert await FEE_LOCKBOX.fns.owed(PATH_USD, operator).call(w3, to=lockbox) == fees * 55 // 100


class TestBridgeAndSwapper:
    """The L1 BridgedNVNM token and the GuardedSwapper buyback-market wrapper."""

    async def test_bridged_nvnm_only_bridge_mints_and_burns(self, w3, chain_id, funded_account):
        """The L1 NVNM: supply moves only through a Safe-curated BRIDGE-role gateway."""
        owner = funded_account
        token = await create3(w3, chain_id, owner, BRIDGED_NVNM_BYTECODE + encode(["address"], [owner.address]).hex())

        async def send(signer, fn):
            return await send_call(w3, chain_id, signer, token, fn.data)

        async def balance_of(who):
            return await ERC20.fns.balanceOf(who).call(w3, to=token)

        stranger, bridge = new_account(), await funded(w3)
        mint = BRIDGED_NVNM.fns.bridgeMint(stranger.address, ETHER)
        await rejects(w3, token, mint, "EnumerableRolesUnauthorized", sender=stranger.address)

        await send(owner, BRIDGED_NVNM.fns.setRole(bridge.address, 1, True))  # owner grants BRIDGE
        await send(bridge, BRIDGED_NVNM.fns.bridgeMint(bridge.address, 100 * ETHER))
        assert await balance_of(bridge.address) == 100 * ETHER
        await send(bridge, BRIDGED_NVNM.fns.bridgeBurn(bridge.address, 40 * ETHER))
        assert await balance_of(bridge.address) == 60 * ETHER
        assert await ERC20.fns.totalSupply().call(w3, to=token) == 60 * ETHER  # burn shrank supply

    async def test_a_routers_buyback_swaps_through_the_guard_to_the_sink(self, w3, chain_id, funded_account):
        """A router's buyback cut becomes NVNM at the sink, one guard-capped swap per flush; a swap
        under the price floor leaves the cut held on the router. Nobody swaps directly."""
        owner = funded_account

        async def send(to, fn):
            return await send_call(w3, chain_id, owner, to, fn.data)

        usd = await tip20(w3, chain_id, owner, "USD", amount=10_000 * ETHER)
        nvnm = await tip20(w3, chain_id, owner, "NVNM", currency="NVNM", amount=10_000 * ETHER)
        staking = await deploy(w3, chain_id, owner, reward_token=usd, stake_token=nvnm)
        factory, router, _ = await fee_router(
            staking, owner, new_account().address, new_account().address, 10_000, new_account().address
        )

        # A 1:1 pool behind a guard (cap 20, -3% floor, EMA alpha 20%, 10% drift band) at 1.0.
        pool = await create3(w3, chain_id, owner, POOL_BYTECODE + encode(["address", "address"], [usd, nvnm]).hex())
        for tok in (usd, nvnm):
            await send(tok, ERC20.fns.transfer(pool, 1_000 * ETHER))
        guard_arg = encode(["address", "address", "address"], [owner.address, usd, nvnm]).hex()
        guard = await create3(w3, chain_id, owner, GUARDED_SWAPPER_BYTECODE + guard_arg)
        await send(guard, GUARDED_SWAPPER.fns.setGuards(pool, 20 * ETHER, 300, 2_000))
        await send(guard, GUARDED_SWAPPER.fns.setDriftBand(1_000))
        await send(guard, GUARDED_SWAPPER.fns.seedPrice(ETHER))
        await send(guard, GUARDED_SWAPPER.fns.setRouterFactory(factory))
        await send(factory, FEE_ROUTER_FACTORY.fns.setSwapper(guard, SWAP_GAS))

        # 100 in fees: a 25 cut, of which one flush swaps the cap and holds the rest.
        await send(usd, ERC20.fns.transfer(router, 100 * ETHER))
        await send(router, FEE_ROUTER.fns.flush())
        held = FEE_ROUTER.fns.heldForBuyback(usd)
        assert await staking.call(held, to=router) == 5 * ETHER
        bought = await staking.balance(nvnm, SINK)
        assert bought > 19 * ETHER and await staking.balance(usd, SINK) == 0

        # Buying NVNM straight from the pool prices the next swap under the floor: the cut waits.
        await send(usd, ERC20.fns.approve(pool, 500 * ETHER))
        await send(pool, GUARDED_SWAPPER.fns.swap(usd, nvnm, 500 * ETHER, 0))  # the pool's `swap` is the same
        await send(router, FEE_ROUTER.fns.flush())
        assert await staking.call(held, to=router) == 5 * ETHER
        assert await staking.balance(nvnm, SINK) == bought

        # The owner may not swap directly either: a direct caller keeps the output.
        await send(usd, ERC20.fns.approve(guard, ETHER))
        await rejects(w3, guard, GUARDED_SWAPPER.fns.swap(usd, nvnm, ETHER, 0), "NotAuthorized", sender=owner.address)
