"""TIP-1045 payment-lane classification (T5): the general (non-payment) lane is
capped at mainBlockGeneralGasLimit (30M)/block while payment transfers use the
full ~500M block. The lane bit isn't surfaced over RPC, so we assert the
consequence: under load, general txs defer ~1/block while payments cluster.
"""

import asyncio

import pytest
from tempo import Signer, serialize, sign_transaction
from tempo.constants import PATH_USD

from .utils import DEFAULT_GAS_LIMIT, build_tempo_tx, fund, new_account, suggested_max_fee, transfer_call

pytestmark = pytest.mark.tempo

GENERAL_GAS_LIMIT = 30_000_000
DEAD = "0x000000000000000000000000000000000000dEaD"  # a call to a non-TIP-20 target is "general"


async def _send_raw(w3, tx, pk):
    return await w3.eth.send_raw_transaction(serialize(sign_transaction(tx, Signer(pk))))


async def test_general_lane_is_capped_while_payments_are_not(w3, chain_id):
    blk = (await w3.provider.make_request("eth_getBlockByNumber", ["latest", False]))["result"]
    assert int(blk["mainBlockGeneralGasLimit"], 16) == GENERAL_GAS_LIMIT  # lane cap exposed in the header
    assert int(blk["gasLimit"], 16) > GENERAL_GAS_LIMIT  # block is far larger; payments can use it

    spammer, payer = new_account(), new_account()
    await fund(w3, spammer.address)
    await fund(w3, payer.address)
    max_fee = await suggested_max_fee(w3)

    # each general tx reserves the 30M cap, so at most one fits per block -> they defer across blocks
    general = [
        build_tempo_tx(
            chain_id=chain_id,
            nonce=i,
            fee_token=PATH_USD,
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
            fee_token=PATH_USD,
            gas_limit=DEFAULT_GAS_LIMIT,
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
