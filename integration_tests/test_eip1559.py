"""EIP-1559 fee market behavior."""

import pytest
from tempo import Signer, serialize, sign_transaction

from .utils import build_tempo_tx, new_account, send_calls, suggested_max_fee, transfer_call


async def test_block_has_base_fee(w3):
    assert (await w3.eth.get_block("latest"))["baseFeePerGas"] > 0


async def test_effective_gas_price_within_max_fee(w3, chain_id, funded_account):
    max_fee = await suggested_max_fee(w3)
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 1)]
    )
    assert receipt["status"] == 1
    assert receipt["effectiveGasPrice"] <= max_fee


async def test_max_fee_below_base_fee_is_rejected(w3, chain_id, funded_account):
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        max_fee_per_gas=1,
        max_priority_fee_per_gas=1,
        calls=[transfer_call(new_account().address, 1)],
    )
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))
