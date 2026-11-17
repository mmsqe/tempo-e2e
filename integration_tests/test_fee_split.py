"""From NVNM's N1, FeeManager sets a quarter of every fee aside for devshare and a quarter for buyback, each in
its own slot, credits the rest to the block's proposer, and closes the validators' payouts."""

import json

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD

from .abi import FEE
from .network import default_genesis, dev_node, patched_node, running, xtask_nvnm_forks
from .utils import active_forks, call_revert, funded, latest_timestamp, new_account, rpc, send_call

pytestmark = pytest.mark.tempo

# Where FeeManager keeps each share's nUSD: a token-to-amount mapping at the hash of its name.
DEVSHARE, BUYBACK = (
    keccak(bytes.fromhex(PATH_USD[2:]).rjust(32, b"\0") + keccak(text=f"nvnm.fee-split.{share}"))
    for share in ("devshare", "buyback")
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


async def one_fee(w3, payer) -> tuple[str, list[int]]:
    """The proposer of a block holding one transfer alone, and what its fee adds to the proposer's ledger and to
    the devshare and buyback slots."""
    transfer = ERC20.fns.transfer(new_account().address, 1).data
    n = (await send_call(w3, await w3.eth.chain_id, payer, PATH_USD, transfer))["blockNumber"]
    proposer = (await w3.eth.get_block(n))["miner"]

    async def held(block: int) -> list[int]:
        kept = await FEE.fns.collectedFees(proposer, PATH_USD).call(w3, block_identifier=block)
        shares = [await w3.eth.get_storage_at(FEE_MANAGER_ADDRESS, slot, block) for slot in (DEVSHARE, BUYBACK)]
        return [kept, *(int.from_bytes(share) for share in shares)]

    return proposer, [after - before for before, after in zip(await held(n - 1), await held(n))]


async def assert_split_and_locked(w3, payer) -> None:
    # `tempo_forkSchedule` lists the lane's fork, never its `Baseline`.
    assert {"BASELINE", "N1"} & await active_forks(w3) == {"N1"}
    proposer, (kept, dev, buy) = await one_fee(w3, payer)
    assert dev == buy == (kept + dev + buy) * 2_500 // 10_000 > 0
    assert FEES_LOCKED in await call_revert(w3, FEE.fns.distributeFees(proposer, PATH_USD))


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
