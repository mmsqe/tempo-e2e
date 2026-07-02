"""TIP-1020 SignatureVerifier (0x5165…): on-chain recover/verify of a tempo
signature; verifyKeychainAdmin (T6+) checks an account's keychain keys.
"""

import pytest
from eth_utils import keccak
from tempo import Signer
from tempo.constants import SIGNATURE_VERIFIER_ADDRESS as SV
from tempo.keychain import build_keychain_signature

from .abi import SIGNATURE_VERIFIER as SIG
from .utils import call_revert, new_account

pytestmark = pytest.mark.tempo

HASH = b"\xab" * 32
INVALID_FORMAT = "0x257aa23b"  # InvalidFormat()


def _signer() -> Signer:
    return Signer(new_account().key.hex())


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
