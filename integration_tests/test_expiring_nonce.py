"""TIP-1009 expiring nonces: nonce_key = uint256 max with a required valid_before;
replay protection is hash-based and the protocol (key 0) nonce is untouched.
"""

import pytest
from tempo import Signer, serialize, sign_transaction
from tempo.constants import EXPIRING_NONCE_KEY, PATH_USD

from .utils import build_tempo_tx, fund, latest_timestamp, new_account, send_tempo_tx, suggested_max_fee, transfer_call

pytestmark = pytest.mark.tempo

MAX_EXPIRY_SECS = 30  # EXPIRING_NONCE_MAX_EXPIRY_SECS: valid_before must land in (now, now+30]


def _expiring_tx(chain_id, *, valid_before, max_fee, nonce=0):
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=nonce,
        nonce_key=EXPIRING_NONCE_KEY,
        valid_before=valid_before,
        fee_token=PATH_USD,
        max_fee_per_gas=max_fee,
        calls=[transfer_call(new_account().address, 1)],
    )


def _raw(tx, acct):
    return serialize(sign_transaction(tx, Signer(acct.key.hex())))


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
    with pytest.raises(Exception):  # below the pool floor (tip + 3s)
        await w3.eth.send_raw_transaction(_raw(tx, acct))


async def test_replayed_expiring_nonce_is_rejected(w3, chain_id):
    acct = new_account()
    await fund(w3, acct.address)
    tx = _expiring_tx(chain_id, valid_before=await latest_timestamp(w3) + 15, max_fee=await suggested_max_fee(w3))
    raw = _raw(tx, acct)

    first = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw))
    assert first["status"] == 1
    with pytest.raises(Exception):  # identical tx hash already seen
        await w3.eth.send_raw_transaction(raw)


async def test_window_beyond_max_expiry_is_rejected(w3, chain_id):
    """E3: valid_before further than 30s out is outside (now, now+30] -> InvalidExpiringNonceExpiry."""
    acct = new_account()
    await fund(w3, acct.address)
    far = await latest_timestamp(w3) + MAX_EXPIRY_SECS + 90
    tx = _expiring_tx(chain_id, valid_before=far, max_fee=await suggested_max_fee(w3))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(_raw(tx, acct))


async def test_nonzero_nonce_is_rejected(w3, chain_id):
    """E4: an expiring-nonce tx must carry nonce == 0 (replay protection is hash-based)."""
    acct = new_account()
    await fund(w3, acct.address)
    soon = await latest_timestamp(w3) + 15
    tx = _expiring_tx(chain_id, valid_before=soon, max_fee=await suggested_max_fee(w3), nonce=1)
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(_raw(tx, acct))


async def test_missing_valid_before_is_rejected(w3, chain_id):
    """E5: the expiring key without a valid_before has no expiry to record -> rejected."""
    acct = new_account()
    await fund(w3, acct.address)
    tx = _expiring_tx(chain_id, valid_before=None, max_fee=await suggested_max_fee(w3))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(_raw(tx, acct))
