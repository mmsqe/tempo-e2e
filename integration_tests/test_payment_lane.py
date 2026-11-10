"""TIP-1045 payment lane (T5): general txs share a 30M cap per block, payments use the whole block.

The lane isn't exposed over RPC, so the test checks the effect: general txs defer, payments cluster.
"""

import asyncio
import contextlib
from pathlib import Path

import pytest
from tempo import Signer, serialize, sign_transaction
from web3.exceptions import TimeExhausted

from .network import fork_schedule, generate_dev_genesis, patched_node, running, xtask_forks
from .utils import FAR_FUTURE, build_tempo_tx, funded, new_account, rpc, suggested_max_fee, transfer_call

pytestmark = pytest.mark.tempo

GENERAL_GAS_LIMIT = 30_000_000
DEAD = "0x000000000000000000000000000000000000dEaD"  # a call to a non-TIP-20 target is "general"
TAG = b"\xab" * 32
TAGGED_GAS = 1_000_000
UNMINED_AFTER = 5  # seconds, a hundred 50 ms blocks


async def _send_raw(w3, tx, pk):
    return await w3.eth.send_raw_transaction(serialize(sign_transaction(tx, Signer(pk))))


async def test_general_lane_is_capped_while_payments_are_not(w3, chain_id):
    blk = (await w3.provider.make_request("eth_getBlockByNumber", ["latest", False]))["result"]
    assert int(blk["mainBlockGeneralGasLimit"], 16) == GENERAL_GAS_LIMIT  # lane cap exposed in the header
    assert int(blk["gasLimit"], 16) > GENERAL_GAS_LIMIT  # block is far larger; payments can use it

    spammer, payer = await funded(w3), await funded(w3)
    max_fee = await suggested_max_fee(w3)

    # each general tx reserves the 30M cap, so at most one fits per block -> they defer across blocks
    general = [
        build_tempo_tx(
            chain_id=chain_id,
            nonce=i,
            gas_limit=GENERAL_GAS_LIMIT,
            max_fee_per_gas=max_fee,
            calls=[{"to": DEAD, "data": b""}],
        )
        for i in range(4)
    ]
    # pure TIP-20 transfers are payment-lane and are not throttled by the general cap
    payments = [
        build_tempo_tx(
            chain_id=chain_id,
            nonce=i,
            max_fee_per_gas=max_fee,
            calls=[transfer_call(new_account().address, 1)],
        )
        for i in range(8)
    ]

    # submit all at once so they co-reside in the pool for the same block builds
    g_hashes = await asyncio.gather(*[_send_raw(w3, tx, spammer.key.hex()) for tx in general])
    p_hashes = await asyncio.gather(*[_send_raw(w3, tx, payer.key.hex()) for tx in payments])
    g_receipts = await asyncio.gather(*[w3.eth.wait_for_transaction_receipt(h) for h in g_hashes])
    p_receipts = await asyncio.gather(*[w3.eth.wait_for_transaction_receipt(h) for h in p_hashes])

    g_blocks = {r["blockNumber"] for r in g_receipts}
    p_blocks = {r["blockNumber"] for r in p_receipts}
    assert len(g_blocks) >= 3  # general txs dribble out ~1 per block under the 30M cap
    assert len(p_blocks) <= 3  # payment transfers are not throttled, clustering into few blocks


@contextlib.asynccontextmanager
async def _narrow_lane(base: Path, fork_times: dict[str, int] | None = None):
    """A dev node whose general lane is narrower than ``TAGGED_GAS``, so a transfer declaring it fits only
    as a payment; skips without xtask scheduling T15."""
    if "t15_time" not in xtask_forks():
        pytest.skip("tempo-xtask cannot schedule t15_time")
    genesis = generate_dev_genesis(base / "xtask", fork_times=fork_times)
    node = patched_node(base, genesis, lambda spec: spec["config"].update(generalGasLimit=TAGGED_GAS // 2))
    with running(node):
        async with rpc(node) as w3:
            yield w3


async def _lands_as_payment(w3, tag: bytes) -> bool:
    """Whether a transfer with ``tag`` after its arguments is mined on a ``_narrow_lane`` node."""
    call = transfer_call(new_account().address, 1)
    call["data"] += tag
    tx = build_tempo_tx(
        chain_id=await w3.eth.chain_id,
        gas_limit=TAGGED_GAS,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[call],
    )
    tx_hash = await _send_raw(w3, tx, (await funded(w3)).key.hex())
    try:
        await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=UNMINED_AFTER)
    except TimeExhausted:
        return False
    return True


async def test_from_t15_a_transfer_may_carry_a_32_byte_tag(tmp_path):
    """#8850: integrators append an attribution tag to TIP-20 transfers."""
    async with _narrow_lane(tmp_path) as w3:
        assert await _lands_as_payment(w3, TAG)
        assert not await _lands_as_payment(w3, TAG + b"\xab")


async def test_before_t15_a_tagged_transfer_is_general(tmp_path):
    async with _narrow_lane(tmp_path, fork_schedule("t15_time", FAR_FUTURE)) as w3:
        assert await _lands_as_payment(w3, b"")
        assert not await _lands_as_payment(w3, TAG)
