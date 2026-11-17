"""From NVNM's N1, FeeManager splits every fee between the block's proposer and its devshare and buyback ledger
keys, and holds all of it in the fee ledger for a later fork to open."""

import json

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_checksum_address
from tempo.constants import PATH_USD

from .abi import FEE
from .network import default_genesis, dev_node, patched_node, running, xtask_nvnm_forks
from .utils import active_forks, call_revert, funded, latest_timestamp, new_account, rpc, send_call

pytestmark = pytest.mark.tempo

# No key signs for either: a later fork names the wallets.
DEVSHARE, BUYBACK = (
    to_checksum_address(keccak(text=f"nvnm.fee-split.{share}")[-20:]) for share in ("devshare", "buyback")
)
FEES_LOCKED = "0x" + keccak(text="FeesLocked()")[:4].hex()


@pytest.fixture(scope="module", autouse=True)
def needs_n1():
    if "n1_time" not in xtask_nvnm_forks():
        pytest.skip("tempo-xtask cannot schedule N1")


@pytest.fixture(scope="module")
def new_chain(tmp_path_factory):
    node = dev_node(tmp_path_factory.mktemp("fee-split"), fork_times={"n1_time": 0})
    with running(node):
        yield node


@pytest.fixture(scope="module")
def running_chain(tmp_path_factory):
    """A chain on its own copy of the genesis, which the test rewrites."""
    node = patched_node(tmp_path_factory.mktemp("fee-split-later"), default_genesis(), lambda spec: None)
    with running(node):
        yield node


async def ledger(w3, account: str, block: int) -> int:
    return await FEE.fns.collectedFees(account, PATH_USD).call(w3, block_identifier=block)


async def one_fee(w3, payer) -> tuple[str, list[int]]:
    """The proposer of a block holding one transfer alone, and what its fee adds to the proposer's, devshare's
    and buyback's ledgers."""
    transfer = ERC20.fns.transfer(new_account().address, 1).data
    n = (await send_call(w3, await w3.eth.chain_id, payer, PATH_USD, transfer))["blockNumber"]
    proposer = (await w3.eth.get_block(n))["miner"]
    return proposer, [await ledger(w3, a, n) - await ledger(w3, a, n - 1) for a in (proposer, DEVSHARE, BUYBACK)]


async def assert_split_and_locked(w3, payer) -> None:
    # `tempo_forkSchedule` lists the lane's fork, never its `Baseline`.
    assert {"BASELINE", "N1"} & await active_forks(w3) == {"N1"}
    proposer, (kept, dev, buy) = await one_fee(w3, payer)
    assert dev == buy == (kept + dev + buy) * 2_500 // 10_000 > 0
    for account in (proposer, DEVSHARE, BUYBACK):
        assert FEES_LOCKED in await call_revert(w3, FEE.fns.distributeFees(account, PATH_USD))


async def test_a_new_chain_splits_from_block_one(new_chain):
    async with rpc(new_chain) as w3:
        await assert_split_and_locked(w3, await funded(w3))


async def test_a_running_chain_splits_from_n1(running_chain):
    async with rpc(running_chain) as w3:
        payer = await funded(w3)
        _, (kept, dev, buy) = await one_fee(w3, payer)
        assert kept > 0 and dev == buy == 0
        opens = await latest_timestamp(w3) + 5

    # A release schedules N1 in the genesis config; the node restarts on its own data.
    running_chain.stop()
    spec = json.loads(running_chain.genesis.read_text())
    spec["config"] |= {"n1Time": opens}
    running_chain.genesis.write_text(json.dumps(spec))
    running_chain.start().wait_for_rpc(want_timestamp=opens)

    async with rpc(running_chain) as w3:
        # The proposer's fees from before N1 are held too.
        await assert_split_and_locked(w3, payer)
