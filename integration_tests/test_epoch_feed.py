"""The node's staking-election feed on a localnet whose genesis names the staking proxy.

Needs ``--consensus`` and a binary with the feed; one without it fails rather than skips."""

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS

from .abi import (
    CURRENT_COMMITTEE,
    CURRENT_COMMITTEE_ADDRESS,
    FEE,
    FEE_LOCKBOX,
    FEE_ROUTER,
    STAKING,
    VALIDATOR_CONFIG_V2,
)
from .conftest import LOCALNET_CHAIN_ID, localnet
from .keeper import keep
from .network import FAUCET_PRIVATE_KEY
from .staking import ETHER, STAKING_GENESIS_ADDRESS, STAKING_SALT, deploy, deploy_stack, tip20
from .utils import connect, cs, new_account, send_call, until

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

N_ELECTION_VALIDATORS = 5  # elect 4 of 5: passes the node's min(4, registry) floor
EPOCH_LENGTH = 20  # blocks: short, since one test waits out three boundaries
AMM_SEED = 10**11  # USDT0 the fee AMM holds to pay routers for PATH_USD gas


async def _traffic_until_fees(w3, faucet, recipients, token=PATH_USD):
    """Send cheap txs, paid in PATH_USD, until one of `recipients` has collected fees in `token`;
    return it and the amount."""
    for _ in range(20):
        await send_call(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, ERC20.fns.transfer(faucet.address, 1).data)
        for recipient in recipients:
            if collected := await FEE.fns.collectedFees(recipient, token).call(w3, to=FEE_MANAGER_ADDRESS):
                return recipient, collected
    pytest.fail(f"no chain fees accrued to any of {recipients}")


async def _committee(w3):
    """The live committee as (epoch, {pubkey bytes})."""
    epoch, keys = await CURRENT_COMMITTEE.fns.getCommitteeMembers().call(w3, to=CURRENT_COMMITTEE_ADDRESS)
    return epoch, {bytes(k) for k in keys}


@pytest.fixture(scope="module")
def election_net(request, tmp_path_factory):
    """Five validators whose genesis names the staking proxy as `stakingElection`."""
    genesis = {"stakingElection": STAKING_GENESIS_ADDRESS}
    yield from localnet(
        request, tmp_path_factory, "staking-election", N_ELECTION_VALIDATORS, epoch_length=EPOCH_LENGTH, genesis=genesis
    )


@pytest.fixture
async def w3(election_net):
    client = connect(election_net.node_rpc_url("node0"))
    yield client
    await client.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
class TestEpochFeed:
    """The node-side staking-election feed driving the consensus committee."""

    async def test_distribute_fees_pays_the_collected_amount(self, w3):
        """Tx fees accrue under block proposers' fee recipients; distributeFees pays them out."""
        faucet = Account.from_key(FAUCET_PRIVATE_KEY)
        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
        recipients = [cs(v[4]) for v in active if int(v[4], 16) != 0]
        assert recipients, "localnet validators have no fee recipients"

        pick, collected = await _traffic_until_fees(w3, faucet, recipients)

        before = await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD)
        await send_call(w3, LOCALNET_CHAIN_ID, faucet, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(pick, PATH_USD).data)
        # More fees may accrue for `pick` between the read and the payout, so >=.
        assert await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD) - before >= collected

    async def test_committee_follows_staking_election(self, w3):
        """Below min(4, registry) the node keeps its current committee (fallback), here all five;
        at/above it, the committee shrinks to exactly the elected set."""
        faucet = Account.from_key(FAUCET_PRIVATE_KEY)
        staking = await deploy(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, salt=STAKING_SALT)
        assert staking.address == STAKING_GENESIS_ADDRESS, "proxy != genesis stakingElection address"

        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
        assert len(active) == N_ELECTION_VALIDATORS
        key_by_addr = {cs(v[1]): bytes(v[0]) for v in active}
        ranked = sorted(key_by_addr)  # deterministic order by address
        await staking.setup_election(faucet, [])

        async def elect(addr):
            await staking.send(faucet, STAKING.fns.setCandidate(addr, True))
            await staking.stake(faucet, addr, 100 * ETHER)

        # Phase 1 — 3 of 5 is below the min(4, registry) floor: after two boundaries the committee
        # must still hold all 5, the read having fallen back.
        for addr in ranked[:3]:
            await elect(addr)
        start_epoch, _ = await _committee(w3)

        async def two_boundaries_on():
            epoch, members = await _committee(w3)
            return members if epoch >= start_epoch + 2 else None

        members = await until("two epoch boundaries", two_boundaries_on, timeout=360)
        assert members == set(key_by_addr.values()), "a below-minimum election shrank the committee"

        # Phase 2 — add the 4th: now at the floor, the committee shrinks to exactly those 4.
        await elect(ranked[3])
        expected = {key_by_addr[a] for a in ranked[:4]}

        async def members():
            return (await _committee(w3))[1]

        await until("the committee to be the four elected", members, want=expected, timeout=360)

    async def test_keeper_pays_out_real_chain_fees_in_the_reward_token(self, w3):
        """Real block fees reach the routers in a USDT0 stand-in while users pay gas in PATH_USD.
        Checks conservation, not amounts; runs last, since it repoints every fee recipient."""
        faucet = Account.from_key(FAUCET_PRIVATE_KEY)
        owner = cs(await VALIDATOR_CONFIG_V2.fns.owner().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS))
        assert owner == faucet.address, "faucet must own the registry to repoint fee recipients"
        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)

        usdt0 = cs(await tip20(w3, LOCALNET_CHAIN_ID, faucet, "USDT0", amount=10**12))
        seed = FEE.fns.mint(PATH_USD, usdt0, AMM_SEED, faucet.address)  # PATH_USD in, USDT0 out
        await send_call(w3, LOCALNET_CHAIN_ID, faucet, FEE_MANAGER_ADDRESS, seed.data)

        treasury = new_account().address
        staking, routers, pools, payouts, lockbox = await deploy_stack(
            w3,
            LOCALNET_CHAIN_ID,
            faucet,
            routers=len(active),
            commission_bps=1_000,
            stake=100 * ETHER,
            treasury=treasury,
            validators=active,
            reward_token=usdt0,
        )
        for router in routers:
            assert cs(await FEE.fns.validatorTokens(router).call(w3, to=FEE_MANAGER_ADDRESS)) == usdt0
        await _traffic_until_fees(w3, faucet, routers, token=usdt0)

        collected = [await keep(w3, LOCALNET_CHAIN_ID, faucet, router) for router in routers]
        assert any(collected), "the keeper found nothing to pay out"
        for router in routers:
            unpaid = await FEE.fns.collectedFees(router, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
            assert unpaid == 0, "a router was credited in PATH_USD"

        dev = await staking.balance(usdt0, treasury)
        # No swapper is set, so each router holds its buyback cut.
        buy = sum([await FEE_ROUTER.fns.heldForBuyback(usdt0).call(w3, to=r) for r in routers])
        # Nothing has commenced distribution, so the lockbox holds each remainder whole.
        ops = sum([await FEE_LOCKBOX.fns.owed(usdt0, p).call(w3, to=lockbox) for p in payouts])
        for v in pools:
            await staking.vest(v)
        pool = sum([await staking.earned(v, faucet.address) for v in pools])

        assert dev == buy and dev > 0, "the two protocol cuts are the same 25%"
        assert pool == 0 and ops > 0, "the operators are owed the pools' share too"
        assert abs((dev + buy + ops) // 4 - dev) <= len(routers), "a cut is a quarter, bar rounding"
        for router in routers:
            held = await FEE_ROUTER.fns.heldForBuyback(usdt0).call(w3, to=router)
            assert await staking.balance(usdt0, router) == held, "the keeper left only the buyback cut"
