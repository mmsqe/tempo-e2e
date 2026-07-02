"""TIP-1009 expiring nonces: nonce_key = uint256 max with a required valid_before;
replay protection is hash-based and the protocol (key 0) nonce is untouched.
"""

import pytest
from tempo import Signer, serialize, sign_transaction
from tempo.constants import PATH_USD

from .utils import build_tempo_tx, fund, latest_timestamp, new_account, send_tempo_tx, suggested_max_fee, transfer_call

pytestmark = pytest.mark.tempo

EXPIRING_KEY = 2**256 - 1


def _expiring_tx(chain_id, *, valid_before, max_fee):
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        nonce_key=EXPIRING_KEY,
        valid_before=valid_before,
        fee_token=PATH_USD,
        max_fee_per_gas=max_fee,
        calls=[transfer_call(new_account().address, 1)],
    )


async def test_expiring_nonce_tx_succeeds(w3, chain_id):
    acct = new_account()
    await fund(w3, acct.address)
    tx = _expiring_tx(chain_id, valid_before=await latest_timestamp(w3) + 15, max_fee=await suggested_max_fee(w3))

    receipt = await send_tempo_tx(w3, tx, acct.key.hex())
    assert receipt["status"] == 1
    # E6: an expiring-nonce tx does not advance the protocol (2D key 0) nonce.
    assert await w3.eth.get_transaction_count(acct.address) == 0


async def test_expired_valid_before_is_rejected(w3, chain_id):
    acct = new_account()
    await fund(w3, acct.address)
    tx = _expiring_tx(chain_id, valid_before=await latest_timestamp(w3) - 1, max_fee=await suggested_max_fee(w3))
    raw = serialize(sign_transaction(tx, Signer(acct.key.hex())))
    with pytest.raises(Exception):  # below the pool floor (tip + 3s)
        await w3.eth.send_raw_transaction(raw)


async def test_replayed_expiring_nonce_is_rejected(w3, chain_id):
    acct = new_account()
    await fund(w3, acct.address)
    tx = _expiring_tx(chain_id, valid_before=await latest_timestamp(w3) + 15, max_fee=await suggested_max_fee(w3))
    raw = serialize(sign_transaction(tx, Signer(acct.key.hex())))

    first = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw))
    assert first["status"] == 1
    with pytest.raises(Exception):  # identical tx hash already seen
        await w3.eth.send_raw_transaction(raw)
