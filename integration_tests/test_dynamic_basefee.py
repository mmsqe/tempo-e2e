"""TIP-1067 dynamic base fee: a block over the gas target raises the next block's baseFeePerGas, and
idle blocks decay it toward the floor."""

import asyncio

import pytest

from .network import dev_node
from .utils import STATE_WRITE_GAS, funded, new_account, rpc, send_calls, transfer_call, wait_for_block

pytestmark = pytest.mark.tempo

GAS_TARGET = 10_000_000  # per TIP-1067; a block above this raises the next base fee
BASE_FEE_FLOOR = 600_000_000  # cap / 20
BASE_FEE_CAP = 12_000_000_000


# One AA tx caps at 32 calls (~8M gas), under the 10M target, so congestion needs several txs per
# block; the default block time mines before the mempool fills, so this module runs a 1sec node.
@pytest.fixture(scope="module")
def basefee_node(tmp_path_factory):
    base = tmp_path_factory.mktemp("basefee")
    node = dev_node(base, block_time="1sec")
    try:
        node.start().wait_for_rpc()
        yield node
    finally:
        node.stop()


@pytest.fixture
async def w3(basefee_node):
    async with rpc(basefee_node) as client:
        yield client


@pytest.fixture
def chain_id(basefee_node) -> int:
    return basefee_node.chain_id


def _next_base_fee(fee: int, gas: int) -> int:
    """EIP-1559's integer update (denominator 8) against the fixed 10M target, clamped to [floor, cap]."""
    if gas > GAS_TARGET:
        nxt = fee + max(1, fee * (gas - GAS_TARGET) // GAS_TARGET // 8)
    else:
        nxt = fee - fee * (GAS_TARGET - gas) // GAS_TARGET // 8
    return min(max(nxt, BASE_FEE_FLOOR), BASE_FEE_CAP)


async def _load_window(w3, chain_id, *, senders, transfers, tail=8):
    """Fire one tx of ``transfers`` fresh-account creations per sender; return the blocks from just
    before the first receipt to ``tail`` past the last, covering the fee rise and the idle decay."""
    receipts = await asyncio.gather(
        *[
            send_calls(
                w3,
                chain_id=chain_id,
                private_key=s.key.hex(),
                gas_limit=STATE_WRITE_GAS,
                calls=[transfer_call(new_account().address, 1) for _ in range(transfers)],
            )
            for s in senders
        ]
    )
    lo = min(r["blockNumber"] for r in receipts) - 1  # one block before the load, for the transition in
    hi = max(r["blockNumber"] for r in receipts) + tail
    await wait_for_block(w3, hi)
    return [await w3.eth.get_block(n) for n in range(lo, hi + 1)]


async def test_base_fee_rises_on_congestion_and_decays_when_idle(w3, chain_id):
    senders = [await funded(w3) for _ in range(6)]

    # each tx is 30 fresh-account creations (~7.5M gas); a block packs several, well past the 10M target
    blocks = await _load_window(w3, chain_id, senders=senders, transfers=30)
    fee = [b["baseFeePerGas"] for b in blocks]
    gas = [b["gasUsed"] for b in blocks]

    # a congested block (gas over target) raises the following block's base fee
    assert any(gas[i] > GAS_TARGET and fee[i + 1] > fee[i] for i in range(len(blocks) - 1)), (gas, fee)
    # an idle block (no gas), once the fee is above the floor, decays the following block's fee
    assert any(gas[i] == 0 and fee[i + 1] < fee[i] for i in range(len(blocks) - 1)), (gas, fee)


async def test_base_fee_follows_the_update_formula(w3, chain_id):
    """Every observed transition matches the TIP-1067 integer formula and stays inside [floor, cap]."""
    senders = [await funded(w3) for _ in range(3)]

    blocks = await _load_window(w3, chain_id, senders=senders, transfers=20)
    assert any(b["gasUsed"] > 0 for b in blocks)  # the load landed inside the window
    for prev, nxt in zip(blocks, blocks[1:]):
        assert BASE_FEE_FLOOR <= nxt["baseFeePerGas"] <= BASE_FEE_CAP
        assert nxt["baseFeePerGas"] == _next_base_fee(prev["baseFeePerGas"], prev["gasUsed"]), (
            prev["number"],
            prev["baseFeePerGas"],
            prev["gasUsed"],
        )
