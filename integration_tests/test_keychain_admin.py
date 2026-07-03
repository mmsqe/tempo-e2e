"""TIP-1049 admin keys + TIP-1053 witnesses on AccountKeychain (0xaAAA…, T6/T5):
the root EOA is its own admin key and can register more admin keys; a burned
witness or a revoked key can't reauthorize, and an admin key can't carry limits.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer
from tempo.constants import PATH_USD
from tempo.contracts import ACCOUNT_KEYCHAIN as KC
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS as KC_ADDR
from tempo.keychain import TokenLimit, sign_tx_access_key, sign_tx_registered_key

from .utils import (
    build_tempo_tx,
    call_revert,
    fund,
    key_restrictions,
    new_account,
    prepare_tx,
    send_call,
    send_signed,
    transfer_call,
)

pytestmark = pytest.mark.tempo

SECP256K1 = 0
ZERO_WITNESS = bytes(32)


async def _kc(w3, chain_id, signer, data):
    return await send_call(w3, chain_id, signer, KC_ADDR, data)


def _authorize_admin(key_id):
    return KC.fns.authorizeAdminKey(key_id, SECP256K1, ZERO_WITNESS).data


async def _key_sends(w3, chain_id, root, key, data, *, is_admin):
    """Provision ``key`` inline (admin or not) and use it to send one keychain call; return the receipt."""
    tx = await prepare_tx(w3, chain_id, root, [{"to": KC_ADDR, "data": data}])
    signed = sign_tx_access_key(tx, key.key.hex(), Signer(root.key.hex()), is_admin=is_admin)
    return await send_signed(w3, signed)


async def test_is_admin_key_semantics(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    k2, stranger = new_account().address, new_account().address

    assert await KC.fns.isAdminKey(root.address, root.address).call(w3, to=KC_ADDR)  # root is its own admin
    assert not await KC.fns.isAdminKey(root.address, stranger).call(w3, to=KC_ADDR)  # never authorized
    await _kc(w3, chain_id, root, _authorize_admin(k2))
    assert await KC.fns.isAdminKey(root.address, k2).call(w3, to=KC_ADDR)  # freshly registered admin key


async def test_authorize_admin_key_rejects_self_and_duplicate(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    k2 = new_account().address
    await _kc(w3, chain_id, root, _authorize_admin(k2))

    # keyId == account is rejected
    assert "InvalidKeyId" in await call_revert(w3, KC_ADDR, _authorize_admin(root.address), sender=root.address)
    # re-authorizing an existing key is rejected
    assert "KeyAlreadyExists" in await call_revert(w3, KC_ADDR, _authorize_admin(k2), sender=root.address)


async def test_admin_key_can_authorize_another_admin_key(w3, chain_id):
    root, provisioned, k3 = new_account(), new_account(), new_account().address
    await fund(w3, root.address)
    # `provisioned` is registered as an admin key inline and, in the same tx, authorizes k3
    receipt = await _key_sends(w3, chain_id, root, provisioned, _authorize_admin(k3), is_admin=True)
    assert receipt["status"] == 1
    assert await KC.fns.isAdminKey(root.address, provisioned.address).call(w3, to=KC_ADDR)
    assert await KC.fns.isAdminKey(root.address, k3).call(w3, to=KC_ADDR)  # authorized by a non-root admin key


async def test_non_admin_key_cannot_authorize_admin_key(w3, chain_id):
    root, non_admin, k3 = new_account(), new_account(), new_account().address
    await fund(w3, root.address)
    # a non-admin access key is not an admin caller, so authorizeAdminKey fails the batch
    receipt = await _key_sends(w3, chain_id, root, non_admin, _authorize_admin(k3), is_admin=False)
    assert receipt["status"] == 0
    assert not await KC.fns.isAdminKey(root.address, k3).call(w3, to=KC_ADDR)


async def test_admin_authorizes_non_admin_key(w3, chain_id):
    root, k2 = new_account(), new_account().address
    await fund(w3, root.address)
    restrictions = key_restrictions()  # permissive non-admin key: never expires, allows any call
    await _kc(w3, chain_id, root, KC.fns.authorizeKey(k2, SECP256K1, restrictions).data)
    assert not await KC.fns.isAdminKey(root.address, k2).call(w3, to=KC_ADDR)  # active, but not an admin key
    # re-authorizing the same key confirms it is registered
    dup = KC.fns.authorizeKey(k2, SECP256K1, restrictions).data
    assert "KeyAlreadyExists" in await call_revert(w3, KC_ADDR, dup, sender=root.address)


async def test_registered_non_admin_key_signs_a_transfer(w3, chain_id):
    root, k2 = new_account(), new_account()
    await fund(w3, root.address)
    recipient = new_account().address
    restrictions = key_restrictions()  # permissive non-admin key (allowAnyCalls)
    await _kc(w3, chain_id, root, KC.fns.authorizeKey(k2.address, SECP256K1, restrictions).data)
    assert not await KC.fns.isAdminKey(root.address, k2.address).call(w3, to=KC_ADDR)  # a plain access key

    # the on-chain key signs a real transfer with a bare keychain signature (no inline auth);
    # the node looks k2 up in root's keychain, so root's funds move under k2's authority
    tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, 1234)])
    receipt = await send_signed(w3, sign_tx_registered_key(tx, k2.key.hex(), root.address))
    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1234


async def test_admin_key_revokes_another_key(w3, chain_id):
    root, admin_key, k3 = new_account(), new_account(), new_account().address
    await fund(w3, root.address)
    await _kc(w3, chain_id, root, _authorize_admin(k3))  # root registers admin k3
    assert await KC.fns.isAdminKey(root.address, k3).call(w3, to=KC_ADDR)

    # a second admin key (provisioned inline) revokes k3
    receipt = await _key_sends(w3, chain_id, root, admin_key, KC.fns.revokeKey(k3).data, is_admin=True)
    assert receipt["status"] == 1
    assert not await KC.fns.isAdminKey(root.address, k3).call(w3, to=KC_ADDR)  # revoked by the admin key


async def test_admin_key_rejects_limit_and_scope_mutators(w3, chain_id):
    root, k2 = new_account(), new_account().address
    await fund(w3, root.address)
    await _kc(w3, chain_id, root, _authorize_admin(k2))
    # an admin key carries no limits or call scopes, so every restriction mutator is rejected on-chain.
    # the is-admin check precedes each mutator's own validation, so InvalidKeyId is the reason in all cases.
    scope = (PATH_USD, [])  # CallScope(target, selectorRules=[])
    for data in (
        KC.fns.updateSpendingLimit(k2, PATH_USD, 100).data,
        KC.fns.setAllowedCalls(k2, [scope]).data,
        KC.fns.removeAllowedCalls(k2, PATH_USD).data,
    ):
        assert "InvalidKeyId" in await call_revert(w3, KC_ADDR, data, sender=root.address)


async def test_revoked_key_cannot_be_reauthorized(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    k2 = new_account().address
    await _kc(w3, chain_id, root, _authorize_admin(k2))
    assert await KC.fns.isAdminKey(root.address, k2).call(w3, to=KC_ADDR)

    await _kc(w3, chain_id, root, KC.fns.revokeKey(k2).data)
    assert not await KC.fns.isAdminKey(root.address, k2).call(w3, to=KC_ADDR)  # a revoked key is no longer admin

    assert "KeyAlreadyRevoked" in await call_revert(w3, KC_ADDR, _authorize_admin(k2), sender=root.address)


def test_admin_key_cannot_carry_a_spending_limit(chain_id):
    root, k2 = new_account(), new_account()
    tx = build_tempo_tx(chain_id=chain_id, nonce=0, fee_token=PATH_USD, calls=[transfer_call(new_account().address, 1)])
    # TIP-1049: an admin key must not carry limits or scoped calls (rejected before signing)
    limits = (TokenLimit.create(PATH_USD, 100),)
    with pytest.raises(Exception):
        sign_tx_access_key(tx, k2.key.hex(), Signer(root.key.hex()), is_admin=True, limits=limits)


async def test_witness_burn_round_trip(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    witness = b"\x53" * 32

    assert not await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3, to=KC_ADDR)
    await _kc(w3, chain_id, root, KC.fns.burnKeyAuthorizationWitness(witness).data)
    assert await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3, to=KC_ADDR)

    # a key authorization carrying a burned witness is rejected
    burned = KC.fns.authorizeAdminKey(new_account().address, SECP256K1, witness).data
    assert "KeyAuthorizationWitnessAlreadyBurned" in await call_revert(w3, KC_ADDR, burned, sender=root.address)


async def test_non_admin_key_cannot_burn_witness(w3, chain_id):
    """TIP-1053: burning a witness is admin-gated -- a plain access key's attempt fails
    the batch and leaves the witness unburned."""
    root, non_admin = new_account(), new_account()
    await fund(w3, root.address)
    witness = b"\x77" * 32
    data = KC.fns.burnKeyAuthorizationWitness(witness).data
    receipt = await _key_sends(w3, chain_id, root, non_admin, data, is_admin=False)
    assert receipt["status"] == 0
    assert not await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3, to=KC_ADDR)
