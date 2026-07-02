"""TIP-1049 admin keys + TIP-1053 authorization witnesses on AccountKeychain
(0xaAAA…, T6/T5): the root EOA is its own admin key and can register more admin
keys directly; witnesses can be burned, and a burned witness can't reauthorize.
"""

import pytest
from tempo.contracts import ACCOUNT_KEYCHAIN as KC
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS as KC_ADDR

from .utils import call_revert, fund, new_account, send_call

pytestmark = pytest.mark.tempo

SECP256K1 = 0
ZERO_WITNESS = bytes(32)


async def _read(w3, fn):
    """Evaluate an AccountKeychain view (the SDK helpers are sync; we need await)."""
    return fn.decode(await w3.eth.call({"to": KC_ADDR, "data": fn.data}))


async def _kc(w3, chain_id, signer, data):
    return await send_call(w3, chain_id, signer, KC_ADDR, data)


def _authorize_admin(key_id):
    return KC.fns.authorizeAdminKey(key_id, SECP256K1, ZERO_WITNESS).data


async def test_is_admin_key_semantics(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    k2, stranger = new_account().address, new_account().address

    assert await _read(w3, KC.fns.isAdminKey(root.address, root.address))  # root is its own admin
    assert not await _read(w3, KC.fns.isAdminKey(root.address, stranger))  # never authorized
    await _kc(w3, chain_id, root, _authorize_admin(k2))
    assert await _read(w3, KC.fns.isAdminKey(root.address, k2))  # freshly registered admin key


async def test_authorize_admin_key_rejects_self_and_duplicate(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    k2 = new_account().address
    await _kc(w3, chain_id, root, _authorize_admin(k2))

    # keyId == account is rejected
    self_reason = await call_revert(w3, KC_ADDR, _authorize_admin(root.address), sender=root.address)
    assert "InvalidKeyId" in self_reason or "0xb0aeb53e" in self_reason
    # re-authorizing an existing key is rejected
    dup_reason = await call_revert(w3, KC_ADDR, _authorize_admin(k2), sender=root.address)
    assert "KeyAlreadyExists" in dup_reason or "0xaa1ba2f8" in dup_reason


async def test_witness_burn_round_trip(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    witness = b"\x53" * 32

    assert not await _read(w3, KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness))
    await _kc(w3, chain_id, root, KC.fns.burnKeyAuthorizationWitness(witness).data)
    assert await _read(w3, KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness))

    # a key authorization carrying a burned witness is rejected
    reason = await call_revert(
        w3, KC_ADDR, KC.fns.authorizeAdminKey(new_account().address, SECP256K1, witness).data, sender=root.address
    )
    assert "KeyAuthorizationWitnessAlreadyBurned" in reason or "0xc96f8deb" in reason
