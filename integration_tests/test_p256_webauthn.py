"""P-256 and WebAuthn (passkey) signatures on tempo (``0x76``) txs.

A passkey account's address is derived from the credential's public key, so its owner
holds no private key. The same credential either *is* the sender, or is registered on an
ordinary account's keychain as an access key that spends on that account's behalf.

tempo-py builds every valid envelope; what is assembled here is the deliberately broken
ones, which the SDK is right not to offer -- `WebAuthnSignature` refuses a high-s scalar,
and `sign_webauthn` only ever attests to the hash it was given.
"""

from types import SimpleNamespace

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from tempo.contracts import ACCOUNT_KEYCHAIN as KC
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS as KC_ADDR
from tempo.keychain import SignatureType, sign_tx_registered_key
from tempo.p256 import (
    AT,
    ED,
    P256_N,
    UP,
    UV,
    P256Signer,
    WebAuthnSignature,
    build_authenticator_data,
    build_client_data_json,
    webauthn_message_hash,
)
from tempo.transaction import get_sign_payload

from .abi import KEYCHAIN_VIEWS
from .utils import fund, key_restrictions, new_account, prepare_tx, send_call, send_signed, transfer_call

pytestmark = pytest.mark.tempo

RP_ID = "tempo.test"
ORIGIN = f"https://{RP_ID}"


@pytest.fixture
async def passkey(w3):
    """A credential whose derived address holds the faucet stablecoin."""
    signer = P256Signer.generate()
    await fund(w3, signer.checksum_address)
    return signer


@pytest.fixture
async def root(w3):
    """A funded secp256k1 account, to register passkeys on."""
    account = new_account()
    await fund(w3, account.address)
    return account


async def spend(w3, chain_id, key, recipient, amount, *, account=None, **kwargs):
    """A transfer signed by ``key``: from its own address, or from ``account``'s if it keys one.

    ``kwargs`` go to `sign_tx`.
    """
    tx = await prepare_tx(w3, chain_id, account or key, [transfer_call(recipient, amount)])
    return await send_signed(w3, sign_tx(tx, key, account=account, **kwargs))


async def authorize(w3, chain_id, root, key, key_type):
    data = KC.fns.authorizeKey(key.checksum_address, key_type, key_restrictions()).data
    return await send_call(w3, chain_id, root, KC_ADDR, data)


def sign_tx(tx, signer: P256Signer, *, account=None, webauthn: bool = False, **kwargs):
    """Sign ``tx`` with ``signer``: as the passkey account itself, or as ``account``'s key.

    ``kwargs`` reach the signer -- ``pre_hash`` for P-256, ``flags`` for an assertion.
    """
    if webauthn:
        kwargs.setdefault("rp_id", RP_ID)
    if account is not None:
        return sign_tx_registered_key(tx, signer, account.address, webauthn=webauthn, **kwargs)
    payload = get_sign_payload(tx)
    signature = signer.sign_webauthn(payload, **kwargs) if webauthn else signer.sign(payload, **kwargs)
    return tx._replace_fields(sender_signature=signature, sender_address=signer.address)


def broken_assertion(
    tx,
    signer: P256Signer,
    *,
    challenge: bytes | None = None,
    type_field: str = "webauthn.get",
    flags: int = UP | UV,
) -> WebAuthnSignature:
    """A correctly signed assertion that attests to the wrong thing."""
    payload = get_sign_payload(tx)
    authenticator_data = build_authenticator_data(RP_ID, flags=flags)
    client_data = build_client_data_json(challenge if challenge is not None else payload, ORIGIN, type_field=type_field)
    signed = signer.sign(webauthn_message_hash(authenticator_data, client_data))
    return WebAuthnSignature(signed.r, signed.s, signed.pub_key_x, signed.pub_key_y, authenticator_data, client_data)


def malleable(tx, signer: P256Signer, *, webauthn: bool = False):
    """The ``(r, n-s)`` twin of a valid signature, as raw bytes the SDK would refuse to hold.

    The serializer asks a sender signature only for ``to_bytes()``.
    """
    payload = get_sign_payload(tx)
    signed = signer.sign_webauthn(payload, rp_id=RP_ID) if webauthn else signer.sign(payload)
    raw = bytearray(signed.to_bytes())
    # s follows r in both layouts, counted from the front for P-256 and from the back for
    # WebAuthn, whose assertion data sits in between.
    start, end = (-96, -64) if webauthn else (33, 65)
    raw[start:end] = (P256_N - int.from_bytes(signed.s, "big")).to_bytes(32, "big")
    return SimpleNamespace(to_bytes=lambda: bytes(raw))


class TestAsSender:
    """The passkey is the account: no key material outside the authenticator."""

    @pytest.mark.parametrize("pre_hash", [False, True], ids=["raw", "prehash"])
    async def test_p256_signed_tx_transfers(self, w3, chain_id, passkey, pre_hash):
        recipient = new_account().address

        receipt = await spend(w3, chain_id, passkey, recipient, 4200, pre_hash=pre_hash)

        assert receipt["status"] == 1
        assert receipt["from"] == passkey.checksum_address  # keccak256(x ‖ y)[12:]
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 4200

    async def test_webauthn_signed_tx_transfers(self, w3, chain_id, passkey):
        recipient = new_account().address
        before = await ERC20.fns.balanceOf(passkey.checksum_address).call(w3, to=PATH_USD)

        receipt = await spend(w3, chain_id, passkey, recipient, 7000, webauthn=True)

        assert receipt["status"] == 1
        assert receipt["from"] == passkey.checksum_address
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 7000
        # the fee came out of the passkey account too: it lost more than it sent
        assert await ERC20.fns.balanceOf(passkey.checksum_address).call(w3, to=PATH_USD) < before - 7000

    async def test_nonce_advances_and_replay_is_rejected(self, w3, chain_id, passkey):
        tx = await prepare_tx(w3, chain_id, passkey, [transfer_call(new_account().address, 100)])
        signed = sign_tx(tx, passkey, webauthn=True)

        assert (await send_signed(w3, signed))["status"] == 1
        assert await w3.eth.get_transaction_count(passkey.checksum_address) == 1
        with pytest.raises(Exception, match="nonce too low"):
            await send_signed(w3, signed)

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"challenge": b"\xcd" * 32}, id="challenge-not-tx-hash"),
            pytest.param({"flags": 0}, id="neither-up-nor-uv"),
            pytest.param({"flags": UV | AT}, id="attested-credential-data"),
            pytest.param({"flags": UP | UV | ED}, id="extension-data"),
            pytest.param({"type_field": "webauthn.create"}, id="type-not-webauthn-get"),
        ],
    )
    async def test_assertion_validation_rejects(self, w3, chain_id, passkey, kwargs):
        """Each assertion field the node checks, broken one at a time.

        The P-256 signature over the tampered assertion is still valid, so these fail on
        WebAuthn validation rather than on the curve.
        """
        recipient = new_account().address

        tx = await prepare_tx(w3, chain_id, passkey, [transfer_call(recipient, 1)])
        signed = tx._replace_fields(
            sender_signature=broken_assertion(tx, passkey, **kwargs), sender_address=passkey.address
        )

        with pytest.raises(Exception, match="invalid transaction signature"):
            await send_signed(w3, signed)
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0

    @pytest.mark.parametrize("webauthn", [False, True], ids=["p256", "webauthn"])
    async def test_high_s_is_rejected(self, w3, chain_id, passkey, webauthn):
        """``(r, n-s)`` verifies on the curve just as well, so the node rejects it by rule."""
        recipient = new_account().address
        tx = await prepare_tx(w3, chain_id, passkey, [transfer_call(recipient, 1)])
        tampered = malleable(tx, passkey, webauthn=webauthn)
        signed = tx._replace_fields(sender_signature=tampered, sender_address=passkey.address)

        with pytest.raises(Exception, match="invalid transaction signature"):
            await send_signed(w3, signed)
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0

    async def test_another_credential_spends_only_its_own_address(self, w3, chain_id, passkey):
        """The embedded pubkey *is* the sender, so a stranger's signature cannot spend here.

        The signature is fine; the node just derives a different, unfunded sender from it.
        """
        recipient = new_account().address
        tx = await prepare_tx(w3, chain_id, passkey, [transfer_call(recipient, 1)])

        with pytest.raises(Exception, match="insufficient funds"):
            await send_signed(w3, sign_tx(tx, P256Signer.generate()))
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0


class TestAsAccessKey:
    """The passkey spends for an ordinary account, without becoming the sender.

    ``authorizeKey`` records a ``SignatureType`` next to the key id, and the node checks
    the envelope against it, so a key registered under one scheme can't be used with another.
    """

    @pytest.mark.parametrize(
        "key_type, webauthn", [(SignatureType.P256, False), (SignatureType.WEBAUTHN, True)], ids=["p256", "webauthn"]
    )
    async def test_spends_for_its_account(self, w3, chain_id, root, key_type, webauthn):
        key, recipient = P256Signer.generate(), new_account().address
        await authorize(w3, chain_id, root, key, key_type)

        signature_type, key_id, _expiry, _enforce_limits, is_revoked = await KEYCHAIN_VIEWS.fns.getKey(
            root.address, key.checksum_address
        ).call(w3, to=KC_ADDR)
        assert (signature_type, key_id.lower(), is_revoked) == (int(key_type), key.checksum_address.lower(), False)
        assert not await KC.fns.isAdminKey(root.address, key.checksum_address).call(w3, to=KC_ADDR)

        receipt = await spend(w3, chain_id, key, recipient, 1500, account=root, webauthn=webauthn)

        assert receipt["status"] == 1
        assert receipt["from"] == root.address  # the key authorizes; the account still sends
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1500

    @pytest.mark.parametrize(
        "registered, webauthn",
        [
            (SignatureType.SECP256K1, False),
            (SignatureType.SECP256K1, True),
            (SignatureType.WEBAUTHN, False),
            (SignatureType.P256, True),
        ],
        ids=["secp256k1-vs-p256", "secp256k1-vs-webauthn", "webauthn-vs-p256", "p256-vs-webauthn"],
    )
    async def test_registered_signature_type_is_enforced(self, w3, chain_id, root, registered, webauthn):
        """Every mismatched pairing is refused, naming both the registered and the offered type."""
        key, recipient = P256Signer.generate(), new_account().address
        await authorize(w3, chain_id, root, key, registered)
        offered = SignatureType.WEBAUTHN if webauthn else SignatureType.P256

        match = f"SignatureTypeMismatch.*expected: {int(registered)}, actual: {int(offered)}"
        with pytest.raises(Exception, match=match):
            await spend(w3, chain_id, key, recipient, 1, account=root, webauthn=webauthn)
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0

    async def test_unregistered_key_is_refused(self, w3, chain_id, root):
        recipient = new_account().address

        with pytest.raises(Exception, match="KeyNotFound"):
            await spend(w3, chain_id, P256Signer.generate(), recipient, 1, account=root)
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0

    async def test_revoking_stops_it_spending(self, w3, chain_id, root):
        key, recipient = P256Signer.generate(), new_account().address
        await authorize(w3, chain_id, root, key, SignatureType.P256)

        assert (await spend(w3, chain_id, key, recipient, 500, account=root))["status"] == 1
        await send_call(w3, chain_id, root, KC_ADDR, KC.fns.revokeKey(key.checksum_address).data)

        with pytest.raises(Exception, match="KeyAlreadyRevoked"):
            await spend(w3, chain_id, key, recipient, 500, account=root)
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 500  # only the first transfer
