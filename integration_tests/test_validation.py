"""Transaction validation: malformed or unfunded transactions are rejected."""

import pytest
from tempo import Signer, serialize, sign_transaction

from .utils import build_tempo_tx, new_account, suggested_max_fee, transfer_call


async def _send_signed(w3, tx, private_key):
    return await w3.eth.send_raw_transaction(serialize(sign_transaction(tx, Signer(private_key))))


async def test_wrong_chain_id_is_rejected(w3, chain_id, funded_account):
    tx = build_tempo_tx(
        chain_id=chain_id + 1,
        nonce=0,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1)],
    )
    with pytest.raises(Exception):
        await _send_signed(w3, tx, funded_account.key.hex())


async def test_unfunded_sender_is_rejected(w3, chain_id):
    poor = new_account()  # never funded, cannot pay gas
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1)],
    )
    with pytest.raises(Exception):
        await _send_signed(w3, tx, poor.key.hex())
