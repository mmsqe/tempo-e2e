"""The node-side staking-election feed, on a localnet whose genesis carries it.

Opt-in: ``--consensus`` and a feed-capable binary. Genesis carries the precomputed proxy
address; the node's feed eth_calls ``computeCommittee()`` at a block hash from address(0).
A binary without the feed keeps all validators and fails these rather than skipping.
"""

import asyncio
import os
import time

import pytest
from eth_abi.abi import encode
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS
from tempo.devnet.ports import find_free_base_ports
from web3 import AsyncWeb3
from web3.middleware import ExtraDataToPOAMiddleware

from .abi import (
    CURRENT_COMMITTEE,
    CURRENT_COMMITTEE_ADDRESS,
    FEE,
    STAKING,
    STAKING_DEPLOYER,
    VALIDATOR_CONFIG_V2,
)
from .conftest import _consensus_net_supervisord, _run_devnet_init
from .keeper import keep
from .network import FAUCET_PRIVATE_KEY, resolve_tempo_bin, resolve_xtask_bin
from .staking import (
    DEPLOYER_BYTECODE,
    ETHER,
    GLOBAL_POOL,
    STAKING_PROXY_NONCE,
    Staking,
    create,
    create_address,
    cs,
    deploy_stack,
    transact,
)
from .utils import new_account

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

LOCALNET_CHAIN_ID = 1337  # the election localnet's genesis chain id
N_ELECTION_VALIDATORS = 5  # elect 4 of 5: passes the node's min(4, registry) floor
EPOCH_LENGTH = 20  # blocks; see the fixture — these tests are paced by epoch boundaries


# Dedicated key, kept fresh so its nonce-0 create is StakingDeployer at a known address.
ELECTION_DEPLOYER = Account.from_key(keccak(b"nvm-election-deployer"))


def _staking_genesis_address() -> str:
    return create_address(create_address(ELECTION_DEPLOYER.address, 0), STAKING_PROXY_NONCE)


async def _traffic_until_fees(w3, faucet, recipients, tries=20):
    """Send cheap txs until one of `recipients` has collected, and return (it, its balance).

    Fees only accrue when a proposer whose fee recipient is in the set actually builds a block,
    so this polls rather than sending one tx.
    """
    for _ in range(tries):
        await transact(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, ERC20.fns.transfer(faucet.address, 1))
        for recipient in recipients:
            if collected := await FEE.fns.collectedFees(recipient, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS):
                return recipient, collected
    pytest.fail(f"no chain fees accrued to any of {recipients}")


async def _fee_stack_with_traffic(w3, faucet, *, commission_bps=1_000, stake=100 * ETHER):
    """The whole fee side stood up on the localnet, with real fees waiting on it.

    Deploys a router per validator keyed to `GLOBAL_POOL`, repoints every fee recipient at its
    own, then sends traffic until a proposer has actually earned. Returns everything the
    assertions need: (staking, routers, payouts, treasury, buybacks).
    """
    owner = cs(await VALIDATOR_CONFIG_V2.fns.owner().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS))
    assert owner == faucet.address, "faucet must own the registry to repoint fee recipients"

    active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
    treasury, buybacks = new_account().address, new_account().address
    staking, routers, payouts = await deploy_stack(
        w3,
        LOCALNET_CHAIN_ID,
        faucet,
        routers=len(active),
        commission_bps=commission_bps,
        stake=stake,
        split=(treasury, buybacks),
        validators=active,
    )

    await _traffic_until_fees(w3, faucet, routers)
    return staking, routers, payouts, treasury, buybacks


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
        await transact(w3, LOCALNET_CHAIN_ID, faucet, FEE_MANAGER_ADDRESS, FEE.fns.distributeFees(pick, PATH_USD))
        # More fees may accrue for `pick` between the read and the payout, so >=.
        assert await ERC20.fns.balanceOf(pick).call(w3, to=PATH_USD) - before >= collected

    async def test_committee_follows_staking_election(self, election_w3):
        """Below min(4, registry) the node keeps the full registry (fallback); at/above it, the
        committee shrinks to exactly the elected set."""
        w3 = election_w3
        deployer, faucet = ELECTION_DEPLOYER, Account.from_key(FAUCET_PRIVATE_KEY)
        # Fund fresh deployer, its nonce-0 create must land on the genesis-patched address.
        await transact(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, ERC20.fns.transfer(deployer.address, 10**14))
        assert await w3.eth.get_transaction_count(deployer.address) == 0, "election deployer must be fresh"
        init = DEPLOYER_BYTECODE + encode(["address"], [PATH_USD]).hex()
        d = await create(w3, LOCALNET_CHAIN_ID, deployer, init)
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

    async def test_keeper_pays_out_real_chain_fees(self, election_w3):
        """The waterfall end to end on fees the chain actually produced, run by the keeper.

        Every other FeeRouter test hand-transfers the stablecoin to a router and takes it on
        faith that ``distributeFees`` is that same transfer. This closes the loop: the
        validators' fee recipients point at real routers, real block fees accrue there, and the
        shipped keeper pays them out and splits them.

        Conservation rather than exact amounts, since blocks keep landing while the keeper runs
        and what it moved is only knowable from what came out. The arithmetic is asserted on the
        dev node, where the payout is controlled.

        Runs last in the class — it repoints every validator's fee recipient, which the earlier
        tests read.
        """
        w3 = election_w3
        faucet = Account.from_key(FAUCET_PRIVATE_KEY)
        staking, routers, payouts, treasury, buybacks = await _fee_stack_with_traffic(w3, faucet)

        collected = [await keep(w3, LOCALNET_CHAIN_ID, faucet, router) for router in routers]
        assert any(collected), "the keeper found nothing to pay out"

        dev = await staking.balance(PATH_USD, treasury)
        buy = await staking.balance(PATH_USD, buybacks)
        ops = sum([await staking.balance(PATH_USD, p) for p in payouts])
        pool = await staking.earned(GLOBAL_POOL, faucet.address)

        assert dev == buy and dev > 0, "the two protocol cuts are the same 25%"
        assert pool > 0 and ops > 0, "the keeper reached both the operators and the pool"
        assert abs((dev + buy + ops + pool) // 4 - dev) <= len(routers), "a cut is a quarter, bar rounding"
        for router in routers:
            assert await staking.balance(PATH_USD, router) == 0, "the keeper left nothing on a router"
