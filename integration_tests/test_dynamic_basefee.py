"""TIP-1067 dynamic base fee: a block whose gas exceeds the target raises the next
block's baseFeePerGas; idle blocks decay it back toward the floor. A burst of
state-creating transfers congests a block, and the surrounding idle blocks decay.
"""

import asyncio

import pytest
from web3 import AsyncWeb3

from .network import dev_node
from .utils import STATE_WRITE_GAS, fund, new_account, send_calls, transfer_call, wait_for_block

pytestmark = pytest.mark.tempo

GAS_TARGET = 10_000_000  # per TIP-1067; a block above this raises the next base fee
BASE_FEE_FLOOR = 600_000_000  # cap / 20
BASE_FEE_CAP = 12_000_000_000


# A single AA tx is capped at 32 calls (~8M gas), below the 10M target, so congestion needs
# several txs packed into one block. The fast default block time mines before the mempool
# fills, so this module runs on its own 1sec node where concurrent txs accumulate first.
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
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(basefee_node.rpc_url))
    yield client
    await client.provider.disconnect()


@pytest.fixture
def chain_id(basefee_node) -> int:
    return basefee_node.chain_id


def _next_base_fee(fee: int, gas: int) -> int:
    """TIP-1067: EIP-1559's integer update (denominator 8) against the fixed 10M target,
    clamped to [floor, cap]."""
    if gas > GAS_TARGET:
        nxt = fee + max(1, fee * (gas - GAS_TARGET) // GAS_TARGET // 8)
    else:
        nxt = fee - fee * (GAS_TARGET - gas) // GAS_TARGET // 8
    return min(max(nxt, BASE_FEE_FLOOR), BASE_FEE_CAP)


async def _load_window(w3, chain_id, *, senders, transfers, gas_limit, tail=8):
    """Fire one batched tx per sender (each ``transfers`` fresh-account creations), then
    return the contiguous blocks from just before the first load block through ``tail``
    blocks past the last one -- covering the fee rise and the idle decay after.

    The window is derived from the receipts, not a fixed offset from ``start``, so it is
    independent of the dev block time (fast blocks would otherwise outrun the load).
    """
    receipts = await asyncio.gather(
        *[
            send_calls(
                w3,
                chain_id=chain_id,
                private_key=s.key.hex(),
                gas_limit=gas_limit,
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
    senders = [new_account() for _ in range(6)]
    for s in senders:
        await fund(w3, s.address)

    # each tx is 30 fresh-account creations (~7.5M gas); on the 1sec node the concurrent txs
    # accumulate in the mempool, so a block packs several of them well past the 10M target
    blocks = await _load_window(w3, chain_id, senders=senders, transfers=30, gas_limit=STATE_WRITE_GAS)
    fee = [b["baseFeePerGas"] for b in blocks]
    gas = [b["gasUsed"] for b in blocks]

    # a congested block (gas over target) raises the following block's base fee
    assert any(gas[i] > GAS_TARGET and fee[i + 1] > fee[i] for i in range(len(blocks) - 1)), (gas, fee)
    # an idle block (no gas), once the fee is above the floor, decays the following block's fee
    assert any(gas[i] == 0 and fee[i + 1] < fee[i] for i in range(len(blocks) - 1)), (gas, fee)


async def test_base_fee_follows_the_update_formula(w3, chain_id):
    """Every observed transition matches the exact TIP-1067 integer formula, and every
    value stays inside [floor, cap]."""
    senders = [new_account() for _ in range(3)]
    for s in senders:
        await fund(w3, s.address)

    blocks = await _load_window(w3, chain_id, senders=senders, transfers=20, gas_limit=STATE_WRITE_GAS)
    assert any(b["gasUsed"] > 0 for b in blocks)  # the load landed inside the window
    for prev, nxt in zip(blocks, blocks[1:]):
        assert BASE_FEE_FLOOR <= nxt["baseFeePerGas"] <= BASE_FEE_CAP
        assert nxt["baseFeePerGas"] == _next_base_fee(prev["baseFeePerGas"], prev["gasUsed"]), (
            prev["number"],
            prev["baseFeePerGas"],
            prev["gasUsed"],
        )
