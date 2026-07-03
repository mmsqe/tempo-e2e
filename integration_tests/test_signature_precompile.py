"""TIP-1020 SignatureVerifier (0x5165…): on-chain recover/verify of a tempo
signature; verifyKeychain / verifyKeychainAdmin (T6+) check an account's keychain
keys against live AccountKeychain state.
"""

import asyncio

import pytest
from eth_utils import keccak
from tempo import Signer
from tempo.constants import SIGNATURE_VERIFIER_ADDRESS as SV
from tempo.contracts import ACCOUNT_KEYCHAIN as KC
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS as KC_ADDR
from tempo.keychain import build_keychain_signature

from .abi import SIGNATURE_VERIFIER as SIG
from .utils import call_revert, fund, latest_timestamp, new_account, send_call

pytestmark = pytest.mark.tempo

HASH = b"\xab" * 32
INVALID_FORMAT = "0x257aa23b"  # InvalidFormat()


def _signer() -> Signer:
    return Signer(new_account().key.hex())


def _keychain_blob(inner_signer, account_addr):
    """A Keychain V2 blob (0x04 ‖ account ‖ inner) with ``inner_signer`` over the domain hash."""
    addr = bytes.fromhex(account_addr[2:])
    return build_keychain_signature(inner_signer.sign(keccak(b"\x04" + HASH + addr)), addr)


async def _authorize(w3, chain_id, account, key_id, *, is_admin, expiry=4_000_000_000):
    if is_admin:
        data = KC.fns.authorizeAdminKey(key_id, 0, bytes(32)).data
    else:  # non-admin key via the ABI path needs a real (future) expiry
        data = KC.fns.authorizeKey(key_id, 0, (expiry, False, [], True, [])).data
    await send_call(w3, chain_id, account, KC_ADDR, data)


async def test_recover_returns_signer(w3):
    signer = _signer()
    sig = signer.sign(HASH).to_bytes()  # 65-byte secp256k1, no type prefix
    recovered = await SIG.fns.recover(HASH, sig).call(w3, to=SV)
    assert recovered.lower() == "0x" + bytes(signer.address).hex()


async def test_verify_matches_only_the_true_signer(w3):
    signer = _signer()
    sig = signer.sign(HASH).to_bytes()
    assert await SIG.fns.verify(signer.address, HASH, sig).call(w3, to=SV)
    assert not await SIG.fns.verify(new_account().address, HASH, sig).call(w3, to=SV)


async def test_recover_rejects_malformed_signature(w3):
    # length != 65 and no valid type byte -> InvalidFormat()
    reason = await call_revert(w3, SV, SIG.fns.recover(HASH, b"\x00" * 64).data)
    assert "InvalidFormat" in reason or INVALID_FORMAT in reason


async def test_verify_keychain_admin_accepts_root_key(w3):
    # A Keychain V2 blob (0x04 ‖ root ‖ inner) signed by the root key; root is its own admin key.
    root = _signer()
    inner = root.sign(keccak(b"\x04" + HASH + bytes(root.address)))
    blob = build_keychain_signature(inner, root.address)
    assert await SIG.fns.verifyKeychainAdmin(root.address, HASH, blob).call(w3, to=SV)


async def test_verify_keychain_rejects_plain_signature(w3):
    # a plain secp256k1 sig is not a keychain V2 blob -> InvalidFormat()
    signer = _signer()
    reason = await call_revert(w3, SV, SIG.fns.verifyKeychain(signer.address, HASH, signer.sign(HASH).to_bytes()).data)
    assert "InvalidFormat" in reason or INVALID_FORMAT in reason


async def test_verify_keychain_accepts_active_non_admin_key(w3, chain_id):
    account, key = new_account(), new_account()
    await fund(w3, account.address)
    blob = _keychain_blob(Signer(key.key.hex()), account.address)

    # an unregistered key is not a keychain key
    assert not await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)
    # a registered non-admin key verifies via verifyKeychain, but not verifyKeychainAdmin
    await _authorize(w3, chain_id, account, key.address, is_admin=False)
    assert await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)
    assert not await SIG.fns.verifyKeychainAdmin(account.address, HASH, blob).call(w3, to=SV)
    # revoking it invalidates verifyKeychain
    await send_call(w3, chain_id, account, KC_ADDR, KC.fns.revokeKey(key.address).data)
    assert not await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)


async def test_verify_keychain_admin_accepts_admin_key(w3, chain_id):
    account, key = new_account(), new_account()
    await fund(w3, account.address)
    blob = _keychain_blob(Signer(key.key.hex()), account.address)
    await _authorize(w3, chain_id, account, key.address, is_admin=True)
    assert await SIG.fns.verifyKeychainAdmin(account.address, HASH, blob).call(w3, to=SV)
    assert await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)  # an admin key is also active


async def test_verify_keychain_rejects_account_mismatch(w3, chain_id):
    account, other, key = new_account(), new_account(), new_account()
    await fund(w3, account.address)
    await _authorize(w3, chain_id, account, key.address, is_admin=True)
    blob = _keychain_blob(Signer(key.key.hex()), account.address)  # the blob embeds `account`

    # recover_keychain_key yields the embedded account, which must equal the passed account:
    # the same blob verifies for the account it embeds, but not for a different one
    assert await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)
    assert await SIG.fns.verifyKeychainAdmin(account.address, HASH, blob).call(w3, to=SV)
    assert not await SIG.fns.verifyKeychain(other.address, HASH, blob).call(w3, to=SV)
    assert not await SIG.fns.verifyKeychainAdmin(other.address, HASH, blob).call(w3, to=SV)


async def test_verify_keychain_rejects_expired_key(w3, chain_id):
    account, key = new_account(), new_account()
    await fund(w3, account.address)
    blob = _keychain_blob(Signer(key.key.hex()), account.address)

    # authorizeKey rejects a past expiry, so set a near-future one and let the 1s-block
    # dev chain advance past it -- an expired key is no longer active
    expiry = await latest_timestamp(w3) + 6
    await _authorize(w3, chain_id, account, key.address, is_admin=False, expiry=expiry)
    assert await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)  # active while unexpired

    for _ in range(40):  # bounded poll (~20s cap); the dev node mines every second
        if await latest_timestamp(w3) >= expiry:
            break
        await asyncio.sleep(0.5)
    assert not await SIG.fns.verifyKeychain(account.address, HASH, blob).call(w3, to=SV)  # expired
