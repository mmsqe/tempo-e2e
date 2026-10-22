"""TIP-1049 admin keys and TIP-1053 authorization witnesses on AccountKeychain (T6/T5)."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer
from tempo.constants import PATH_USD
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS as KC_ADDR
from tempo.keychain import TokenLimit, sign_tx_access_key, sign_tx_registered_key

from .abi import ACCOUNT_KEYCHAIN as KC
from .utils import (
    build_tempo_tx,
    call_revert,
    funded,
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


def _authorize_admin(key_id):
    return KC.fns.authorizeAdminKey(key_id, SECP256K1, ZERO_WITNESS).data


async def _key_sends(w3, chain_id, root, key, data, *, is_admin):
    """Provision ``key`` inline (admin or not) and use it to send one keychain call; return the receipt."""
    tx = await prepare_tx(w3, chain_id, root, [{"to": KC_ADDR, "data": data}])
    signed = sign_tx_access_key(tx, key.key.hex(), Signer(root.key.hex()), is_admin=is_admin)
    return await send_signed(w3, signed)


async def _key_transfers(w3, chain_id, root, key, amount):
    """Move ``amount`` with an already-registered key, return the receipt or None if rejected."""
    tx = await prepare_tx(w3, chain_id, root, [transfer_call(new_account().address, amount)])
    try:
        return await send_signed(w3, sign_tx_registered_key(tx, Signer(key.key.hex()), root.address))
    except Exception:
        # A key the node no longer recognizes is refused before inclusion.
        return None


async def test_is_admin_key_semantics(w3, chain_id):
    root = await funded(w3)
    k2, stranger = new_account().address, new_account().address

    assert await KC.fns.isAdminKey(root.address, root.address).call(w3)  # root is its own admin
    assert not await KC.fns.isAdminKey(root.address, stranger).call(w3)  # never authorized
    await send_call(w3, chain_id, root, KC_ADDR, _authorize_admin(k2))
    assert await KC.fns.isAdminKey(root.address, k2).call(w3)  # freshly registered admin key


async def test_authorize_admin_key_rejects_self_and_duplicate(w3, chain_id):
    root = await funded(w3)
    k2 = new_account().address
    await send_call(w3, chain_id, root, KC_ADDR, _authorize_admin(k2))

    # keyId == account is rejected
    assert "InvalidKeyId" in await call_revert(w3, KC_ADDR, _authorize_admin(root.address), sender=root.address)
    # re-authorizing an existing key is rejected
    assert "KeyAlreadyExists" in await call_revert(w3, KC_ADDR, _authorize_admin(k2), sender=root.address)


async def test_admin_key_can_authorize_another_admin_key(w3, chain_id):
    root, provisioned, k3 = await funded(w3), new_account(), new_account().address
    # `provisioned` is registered as an admin key inline and, in the same tx, authorizes k3
    receipt = await _key_sends(w3, chain_id, root, provisioned, _authorize_admin(k3), is_admin=True)
    assert receipt["status"] == 1
    assert await KC.fns.isAdminKey(root.address, provisioned.address).call(w3)
    assert await KC.fns.isAdminKey(root.address, k3).call(w3)  # authorized by a non-root admin key


async def test_non_admin_key_cannot_authorize_admin_key(w3, chain_id):
    root, non_admin, k3 = await funded(w3), new_account(), new_account().address
    # a non-admin access key is not an admin caller, so authorizeAdminKey fails the batch
    receipt = await _key_sends(w3, chain_id, root, non_admin, _authorize_admin(k3), is_admin=False)
    assert receipt["status"] == 0
    assert not await KC.fns.isAdminKey(root.address, k3).call(w3)


async def test_admin_authorizes_non_admin_key(w3, chain_id):
    root, k2 = await funded(w3), new_account().address
    restrictions = key_restrictions()  # permissive non-admin key: never expires, allows any call
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.authorizeKey(k2, SECP256K1, restrictions).data)
    assert not await KC.fns.isAdminKey(root.address, k2).call(w3)  # active, but not an admin key
    # re-authorizing the same key confirms it is registered
    dup = KC.fns.authorizeKey(k2, SECP256K1, restrictions).data
    assert "KeyAlreadyExists" in await call_revert(w3, KC_ADDR, dup, sender=root.address)


async def test_registered_non_admin_key_signs_a_transfer(w3, chain_id):
    root, k2 = await funded(w3), new_account()
    recipient = new_account().address
    restrictions = key_restrictions()  # permissive non-admin key (allowAnyCalls)
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.authorizeKey(k2.address, SECP256K1, restrictions).data)
    assert not await KC.fns.isAdminKey(root.address, k2.address).call(w3)  # a plain access key

    # a bare keychain signature with no inline auth: the node finds k2 in root's keychain
    tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, 1234)])
    receipt = await send_signed(w3, sign_tx_registered_key(tx, Signer(k2.key.hex()), root.address))
    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1234


async def test_admin_key_revokes_another_key(w3, chain_id):
    root, admin_key, k3 = await funded(w3), new_account(), new_account().address
    await send_call(w3, chain_id, root, KC_ADDR, _authorize_admin(k3))  # root registers admin k3
    assert await KC.fns.isAdminKey(root.address, k3).call(w3)

    # a second admin key (provisioned inline) revokes k3
    receipt = await _key_sends(w3, chain_id, root, admin_key, KC.fns.revokeKey(k3).data, is_admin=True)
    assert receipt["status"] == 1
    assert not await KC.fns.isAdminKey(root.address, k3).call(w3)  # revoked by the admin key


async def test_admin_key_rejects_limit_and_scope_mutators(w3, chain_id):
    root, k2 = await funded(w3), new_account().address
    await send_call(w3, chain_id, root, KC_ADDR, _authorize_admin(k2))
    # the is-admin check runs before each mutator's own validation, so every one fails with InvalidKeyId
    scope = (PATH_USD, [])  # CallScope(target, selectorRules=[])
    for data in (
        KC.fns.updateSpendingLimit(k2, PATH_USD, 100).data,
        KC.fns.setAllowedCalls(k2, [scope]).data,
        KC.fns.removeAllowedCalls(k2, PATH_USD).data,
    ):
        assert "InvalidKeyId" in await call_revert(w3, KC_ADDR, data, sender=root.address)


async def test_revocation_ends_the_key_id_not_its_witness(w3, chain_id):
    """The witness stays replayable for a fresh key id until it is burned."""
    root = await funded(w3)
    k2 = new_account().address
    witness = b"\x5b" * 32
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.authorizeAdminKey(k2, SECP256K1, witness).data)
    assert await KC.fns.isAdminKey(root.address, k2).call(w3)

    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.revokeKey(k2).data)
    assert not await KC.fns.isAdminKey(root.address, k2).call(w3)  # a revoked key is no longer admin

    # the key id is refused for good, whatever witness the retry carries
    assert "KeyAlreadyRevoked" in await call_revert(w3, KC_ADDR, _authorize_admin(k2), sender=root.address)

    # ...but the witness is untouched, so it still authorizes a different key id
    assert not await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3)
    k3 = new_account().address
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.authorizeAdminKey(k3, SECP256K1, witness).data)
    assert await KC.fns.isAdminKey(root.address, k3).call(w3)


def test_admin_key_cannot_carry_a_spending_limit(chain_id):
    root, k2 = new_account(), new_account()
    tx = build_tempo_tx(chain_id=chain_id, calls=[transfer_call(new_account().address, 1)])
    # TIP-1049: an admin key must not carry limits or scoped calls (rejected before signing)
    limits = (TokenLimit.create(PATH_USD, 100),)
    with pytest.raises(Exception):
        sign_tx_access_key(tx, k2.key.hex(), Signer(root.key.hex()), is_admin=True, limits=limits)


async def test_witness_burn_retires_the_authorization_not_the_key(w3, chain_id):
    """Only revokeKey ends a key; burning just stops its authorization being replayed."""
    root, k2 = await funded(w3), new_account()
    witness = b"\x53" * 32
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.authorizeAdminKey(k2.address, SECP256K1, witness).data)
    assert (await _key_transfers(w3, chain_id, root, k2, 100))["status"] == 1

    assert not await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3)
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.burnKeyAuthorizationWitness(witness).data)
    assert await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3)

    # the burned authorization cannot be replayed for another key
    burned = KC.fns.authorizeAdminKey(new_account().address, SECP256K1, witness).data
    assert "KeyAuthorizationWitnessAlreadyBurned" in await call_revert(w3, KC_ADDR, burned, sender=root.address)

    # ...but k2 is still admin and still signs
    assert await KC.fns.isAdminKey(root.address, k2.address).call(w3)
    assert (await _key_transfers(w3, chain_id, root, k2, 101))["status"] == 1

    # revocation is what ends it
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.revokeKey(k2.address).data)
    assert not await KC.fns.isAdminKey(root.address, k2.address).call(w3)
    receipt = await _key_transfers(w3, chain_id, root, k2, 102)
    assert receipt is None or receipt["status"] == 0


async def test_non_admin_key_cannot_burn_witness(w3, chain_id):
    """A plain access key's burn fails the batch and leaves the witness unburned."""
    root, non_admin = await funded(w3), new_account()
    witness = b"\x77" * 32
    data = KC.fns.burnKeyAuthorizationWitness(witness).data
    receipt = await _key_sends(w3, chain_id, root, non_admin, data, is_admin=False)
    assert receipt["status"] == 0
    assert not await KC.fns.isKeyAuthorizationWitnessBurned(root.address, witness).call(w3)
