"""From NVNM's N1, FeeManager sets a quarter of every fee aside for devshare and a quarter for buyback, each in
its own slot, credits the rest to the block's proposer, and closes the validators' payouts. Anyone can have the
two shares paid to the wallets the genesis config names."""

import json

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD

from .abi import FEE
from .network import default_genesis, dev_node, patched_node, resolve_tempo_bin, run_binary, running, xtask_nvnm_forks
from .utils import (
    active_forks,
    call_revert,
    funded,
    latest_timestamp,
    new_account,
    rpc,
    send_call,
    send_calls,
    wait_for_block,
)

pytestmark = pytest.mark.tempo

# Where FeeManager keeps each share's nUSD: a token-to-amount mapping at the hash of its name.
DEVSHARE, BUYBACK = (
    keccak(bytes.fromhex(PATH_USD[2:]).rjust(32, b"\0") + keccak(text=f"nvnm.fee-split.{share}"))
    for share in ("devshare", "buyback")
)
WALLETS = new_account().address, new_account().address
FEES_LOCKED, FEE_WALLET_NOT_SET = (
    "0x" + keccak(text=f"{error}()")[:4].hex() for error in ("FeesLocked", "FeeWalletNotSet")
)


@pytest.fixture(scope="module", autouse=True)
def needs_n1():
    if "n1_time" not in xtask_nvnm_forks():
        pytest.skip("tempo-xtask cannot schedule N1")


@pytest.fixture(scope="module")
def new_chain(tmp_path_factory):
    node = dev_node(tmp_path_factory.mktemp("fee-split"), fork_times={"n1_time": 0}, fee_wallets=WALLETS)
    with running(node):
        yield node


@pytest.fixture(scope="module")
def running_chain(tmp_path_factory):
    """A chain on its own copy of the genesis, which the test rewrites."""
    node = patched_node(tmp_path_factory.mktemp("fee-split-later"), default_genesis(), lambda spec: None)
    with running(node):
        yield node


async def shares(w3, block: int) -> list[int]:
    """What the devshare and buyback slots hold at ``block``."""
    held = [await w3.eth.get_storage_at(FEE_MANAGER_ADDRESS, slot, block) for slot in (DEVSHARE, BUYBACK)]
    return [int.from_bytes(share) for share in held]


async def one_fee(w3, payer, to: str = PATH_USD, data: bytes | None = None) -> tuple[int, str, list[int]]:
    """A block holding one call alone, a transfer unless given: the block, its proposer, and what it adds to
    the proposer's ledger and to the devshare and buyback slots."""
    data = data or ERC20.fns.transfer(new_account().address, 1).data
    n = (await send_call(w3, await w3.eth.chain_id, payer, to, data))["blockNumber"]
    proposer = (await w3.eth.get_block(n))["miner"]

    async def held(block: int) -> list[int]:
        kept = await FEE.fns.collectedFees(proposer, PATH_USD).call(w3, block_identifier=block)
        return [kept, *await shares(w3, block)]

    return n, proposer, [after - before for before, after in zip(await held(n - 1), await held(n))]


def split(kept: int, dev: int, buy: int) -> bool:
    """Whether a fee went a quarter each to devshare and buyback and the rest to the proposer."""
    return dev == buy == (kept + dev + buy) * 2_500 // 10_000 > 0


async def assert_split_and_locked(w3, payer) -> None:
    # `tempo_forkSchedule` lists the lane's fork, never its `Baseline`.
    assert {"BASELINE", "N1"} & await active_forks(w3) == {"N1"}
    _, proposer, fee = await one_fee(w3, payer)
    assert split(*fee)
    assert FEES_LOCKED in await call_revert(w3, FEE.fns.distributeFees(proposer, PATH_USD))


async def test_a_new_chain_splits_from_block_one(new_chain):
    async with rpc(new_chain) as w3:
        await assert_split_and_locked(w3, await funded(w3))


async def assert_claim_pays(w3, payer) -> None:
    """``payer``'s claim empties the two slots into ``WALLETS``, empty until then."""
    claim = FEE.fns.claimFeeShares(PATH_USD).data
    n, _, (kept, dev, buy) = await one_fee(w3, payer, FEE_MANAGER_ADDRESS, claim)
    paid = [await ERC20.fns.balanceOf(wallet).call(w3, to=PATH_USD, block_identifier=n) for wallet in WALLETS]
    assert paid == await shares(w3, n - 1) > [0, 0]
    # The slots lost all of it, then took the claim's own fee, split like any other.
    assert split(kept, dev + paid[0], buy + paid[1])


async def test_anyone_pays_the_shares_to_their_wallets(new_chain):
    async with rpc(new_chain) as w3:
        payer = await funded(w3)
        await one_fee(w3, payer)
        await assert_claim_pays(w3, payer)


async def test_a_wallet_named_after_n1_fails_a_replay_from_genesis(tmp_path):
    """A claim that reverted for want of a wallet is history. Under a config naming one since, it pays instead,
    so a node executing the chain again from genesis refuses that block."""
    node = dev_node(tmp_path, fork_times={"n1_time": 0})
    with running(node):
        async with rpc(node) as w3:
            payer = await funded(w3)
            await one_fee(w3, payer)
            claim = FEE.fns.claimFeeShares(PATH_USD)
            assert FEE_WALLET_NOT_SET in await call_revert(w3, claim)
            calls = [{"to": FEE_MANAGER_ADDRESS, "data": claim.data}]
            reverted = await send_calls(w3, chain_id=node.chain_id, private_key=payer.key.hex(), calls=calls)
            assert reverted["status"] == 0
            n = reverted["blockNumber"]
            await wait_for_block(w3, n + 20)  # until block n persists

    def execute_again(config: dict) -> None:
        spec = json.loads(node.genesis.read_text())
        spec["config"] |= config
        (genesis := tmp_path / "again.json").write_text(json.dumps(spec))
        run_binary(resolve_tempo_bin(), "re-execute", "--chain", str(genesis), "--datadir", str(node.datadir))

    execute_again({})
    with pytest.raises(RuntimeError, match=f"Failed to validate block {n} "):
        execute_again({"feeDevshare": WALLETS[0], "feeBuyback": WALLETS[1]})


async def test_a_running_chain_splits_from_n1(running_chain):
    async with rpc(running_chain) as w3:
        payer = await funded(w3)
        *_, (kept, dev, buy) = await one_fee(w3, payer)
        assert kept > 0 and dev == buy == 0
        opens = await latest_timestamp(w3) + 5

    # A release schedules N1 and names the wallets in the genesis config; the node restarts on its own data.
    running_chain.stop()
    spec = json.loads(running_chain.genesis.read_text())
    spec["config"] |= {"n1Time": opens, "feeDevshare": WALLETS[0], "feeBuyback": WALLETS[1]}
    running_chain.genesis.write_text(json.dumps(spec))
    running_chain.start().wait_for_rpc(want_timestamp=opens)

    async with rpc(running_chain) as w3:
        # The proposer's fees from before N1 are held too.
        await assert_split_and_locked(w3, payer)
        await assert_claim_pays(w3, payer)
