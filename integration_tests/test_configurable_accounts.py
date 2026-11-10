"""T14's configurable accounts (TIP-1112): a weighted owner quorum signs for an address, which an EOA
upgrades into or a configuration derives. The quorum signature is built here from the spec, since
tempo-py has none."""

import os
from dataclasses import dataclass

import pytest
import rlp
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_canonical_address, to_checksum_address
from tempo import Signer, get_sign_payload, serialize, sign_transaction
from tempo.constants import PATH_USD
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS
from tempo.keychain import KeychainSignature, sign_tx_registered_key
from web3.exceptions import TransactionNotFound, Web3RPCError

from .abi import ACCOUNT_KEYCHAIN as KC
from .abi import CONFIGURABLE_ACCOUNT as CA
from .abi import CONFIGURABLE_ACCOUNT_ADDRESS
from .utils import (
    RETURN_42_INIT,
    call_revert,
    deploy_contract,
    emitted,
    fund,
    funded,
    key_restrictions,
    new_account,
    prepare_tx,
    rpc,
    send_call,
    send_calls,
    send_signed,
    skip_before,
    transfer_call,
    wait_for_block,
)

pytestmark = pytest.mark.tempo

SECP256K1, CONFIGURABLE_ACCOUNT = 0, 3
MARKER = b"\xef"  # the code a configurable account holds


@dataclass(frozen=True)
class Envelope:
    """A sender signature tempo-py cannot build, carried as its wire bytes."""

    raw: bytes

    def to_bytes(self) -> bytes:
        return self.raw


@dataclass(frozen=True)
class Multisig:
    """A configurable account and the owner keys of its current configuration, sorted by address as
    the protocol requires."""

    address: str
    salt: bytes
    version: int
    threshold: int
    owners: tuple  # ((LocalAccount, weight), ...)

    @classmethod
    def derived(cls, threshold: int, owners) -> "Multisig":
        """A fresh version-zero configuration and the address it derives."""
        salt = os.urandom(32)
        address = keccak(b"tempo:configurable-account:account" + salt + _set(threshold, owners))[12:]
        return cls(to_checksum_address(address), salt, 0, threshold, owners)

    @classmethod
    def upgraded(cls, root: str, threshold: int, owners) -> "Multisig":
        """The version-one configuration ``upgradeAccount`` gives the EOA ``root``."""
        salt = keccak(b"tempo:configurable-account:upgrade" + to_canonical_address(root))
        return cls(root, salt, 1, threshold, owners)

    @property
    def abi_owners(self) -> tuple:
        return tuple((owner.address, weight) for owner, weight in self.owners)

    @property
    def commitment(self) -> bytes:
        version = self.version.to_bytes(8, "big")
        return keccak(b"tempo:configurable-account:config" + self.salt + version + _set(self.threshold, self.owners))

    def rotate(self, threshold: int, owners):
        """The ``updateConfig`` call to the next version, and the account as it stands after it."""
        current = (self.salt, self.version, self.threshold, self.abi_owners)
        after = Multisig(self.address, self.salt, self.version + 1, threshold, owners)
        return CA.fns.updateConfig(current, threshold, after.abi_owners), after

    def quorum(self, digest: bytes, signers=None) -> bytes:
        """The ``0x05`` envelope in which ``signers`` (all owners by default) approve ``digest``."""
        account, version = to_canonical_address(self.address), self.version.to_bytes(8, "big")
        approved = keccak(b"tempo:configurable-account:signature" + digest + account + version)
        chosen = {s.address for s in signers} if signers is not None else None
        approvals = [
            Signer(owner.key.hex()).sign(approved).to_canonical_bytes()
            for owner, _ in self.owners
            if chosen is None or owner.address in chosen
        ]
        entries = [[to_canonical_address(owner.address), weight] for owner, weight in self.owners]
        return b"\x05" + rlp.encode([account, [self.salt, self.version, self.threshold, entries], approvals])

    def sign(self, tx, signers=None):
        return tx._replace_fields(
            sender_signature=Envelope(self.quorum(get_sign_payload(tx), signers)), sender_address=self.address
        )

    def sign_as_key(self, tx, parent: str):
        """``tx`` from ``parent``, signed by this account as one of its access keys."""
        inner = self.quorum(KeychainSignature.signing_hash(get_sign_payload(tx), parent))
        raw = KeychainSignature(raw=b"\x04" + to_canonical_address(parent) + inner)
        return tx._replace_fields(sender_signature=raw, sender_address=parent)

    async def send(self, w3, chain_id: int, calls, signers=None):
        return await send_signed(w3, self.sign(await prepare_tx(w3, chain_id, self, calls), signers))


def _set(threshold: int, owners) -> bytes:
    """The threshold and owner list as the address and commitment preimages encode them."""
    entries = b"".join(to_canonical_address(owner.address) + bytes([weight]) for owner, weight in owners)
    return bytes([threshold, len(owners)]) + entries


def owners(*weights: int) -> tuple:
    """Fresh owner keys of ``weights``, sorted by address."""
    return tuple(sorted(((new_account(), w) for w in weights), key=lambda o: int(o[0].address, 16)))


async def derived(w3, threshold: int, keys) -> Multisig:
    """A funded, not yet registered, derived account."""
    account = Multisig.derived(threshold, keys)
    await fund(w3, account.address)
    return account


@pytest.fixture
async def t14(w3):
    await skip_before(w3, "T14")


def configured(receipt, event: str) -> list[tuple]:
    """Each ``event`` the precompile logged, its account checksummed and its owners as ``(owner, weight)``."""
    return [
        (to_checksum_address(account), *fields, tuple((o["owner"], o["weight"]) for o in owners))
        for account, *fields, owners in emitted(receipt, CA, event)
    ]


@pytest.mark.usefixtures("t14")
class TestUpgrade:
    """An EOA keeps its address and balance; its owners take over from its key."""

    async def test_an_upgrade_keeps_the_address_and_retires_the_root_key(self, w3, chain_id):
        root, keys = await funded(w3), owners(1, 1)
        account = Multisig.upgraded(root.address, 2, keys)
        receipt = await send_call(w3, chain_id, root, CA.fns.upgradeAccount(2, account.abi_owners))

        [event] = configured(receipt, "AccountUpgraded")
        assert event == (root.address, account.commitment, account.salt, 2, account.abi_owners)
        assert await CA.fns.getConfigCommitment(root.address).call(w3) == account.commitment
        assert await w3.eth.get_code(root.address) == MARKER

        recipient = new_account().address
        with pytest.raises(Web3RPCError, match="is retired and cannot act as the transaction sender"):
            await send_calls(w3, chain_id=chain_id, private_key=root.key.hex(), calls=[transfer_call(recipient, 1)])
        assert (await account.send(w3, chain_id, [transfer_call(recipient, 7)]))["status"] == 1
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 7

    async def test_an_upgrade_may_follow_other_calls_but_nothing_may_reconfigure_after_it(self, w3, chain_id):
        """Since #8778 the upgrade need not be the only call; a later updateConfig undoes the batch."""
        root, keys, recipient = await funded(w3), owners(1), new_account().address
        account = Multisig.upgraded(root.address, 1, keys)
        upgrade = CA.fns.upgradeAccount(1, account.abi_owners)
        rotate, _ = account.rotate(1, owners(1))

        calls = [upgrade, rotate]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=root.key.hex(), calls=calls)
        assert receipt["status"] == 0
        assert await CA.fns.getConfigCommitment(root.address).call(w3) == bytes(32)

        calls = [transfer_call(recipient, 5), upgrade]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=root.key.hex(), calls=calls)
        assert receipt["status"] == 1
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 5
        assert await CA.fns.getConfigCommitment(root.address).call(w3) == account.commitment

    async def test_only_a_plain_account_upgrades_once_to_a_valid_configuration(self, w3, chain_id):
        root = await funded(w3)
        _, contract = await deploy_contract(w3, chain_id=chain_id, private_key=root.key.hex(), bytecode=RETURN_42_INIT)
        account = Multisig.upgraded(root.address, 2, owners(1, 1))
        valid = CA.fns.upgradeAccount(2, account.abi_owners)
        assert "AccountHasCode" in await call_revert(w3, valid, sender=contract)

        unsorted = CA.fns.upgradeAccount(2, account.abi_owners[::-1])
        assert "InvalidConfig" in await call_revert(w3, unsorted, sender=root.address)
        unreachable = CA.fns.upgradeAccount(3, account.abi_owners)
        assert "InvalidConfig" in await call_revert(w3, unreachable, sender=root.address)

        await send_call(w3, chain_id, root, valid)
        assert "AccountAlreadyConfigurable" in await call_revert(w3, valid, sender=root.address)

    async def test_the_pool_drops_what_the_retired_root_key_queued(self, w3, chain_id):
        """#8745: the root key's transaction queued behind its upgrade never lands. It goes in first,
        held by its nonce gap, since a 50 ms block could land the upgrade before it arrived."""
        root = await funded(w3)
        account = Multisig.upgraded(root.address, 1, owners(1))
        upgrade_tx = await prepare_tx(w3, chain_id, root, [CA.fns.upgradeAccount(1, account.abi_owners)])
        later = await prepare_tx(w3, chain_id, root, [transfer_call(new_account().address, 1)])
        signer = Signer(root.key.hex())
        later_hash = await w3.eth.send_raw_transaction(
            serialize(sign_transaction(later._replace_fields(nonce=upgrade_tx.nonce + 1), signer))
        )

        receipt = await send_signed(w3, sign_transaction(upgrade_tx, signer))
        assert receipt["status"] == 1
        await wait_for_block(w3, receipt["blockNumber"] + 20)
        with pytest.raises(TransactionNotFound):
            await w3.eth.get_transaction(later_hash)


@pytest.mark.usefixtures("t14")
class TestQuorum:
    """Which approvals the account accepts."""

    async def test_weights_decide_whether_the_owners_signing_suffice(self, w3, chain_id):
        heavy, light = sorted((new_account(), new_account()), key=lambda owner: int(owner.address, 16))
        account = await derived(w3, 2, ((heavy, 2), (light, 1)))
        transfer = [transfer_call(new_account().address, 1)]

        with pytest.raises(Web3RPCError, match="invalid transaction signature"):
            await account.send(w3, chain_id, transfer, signers=[light])
        # Approvals past the quorum are refused too, and heavy, listed first, reaches it alone.
        with pytest.raises(Web3RPCError, match="invalid transaction signature"):
            await account.send(w3, chain_id, transfer)
        assert (await account.send(w3, chain_id, transfer, signers=[heavy]))["status"] == 1

    async def test_a_derived_account_registers_on_its_first_transaction(self, w3, chain_id):
        account = await derived(w3, 2, owners(1, 1, 1))
        derived_address = await CA.fns.deriveAccount(account.salt, 2, account.abi_owners).call(w3)
        assert to_checksum_address(derived_address) == account.address
        assert await CA.fns.getConfigCommitment(account.address).call(w3) == bytes(32)
        assert await w3.eth.get_code(account.address) == b""

        signers = [owner for owner, _ in account.owners[1:]]
        receipt = await account.send(w3, chain_id, [transfer_call(new_account().address, 1)], signers=signers)
        assert receipt["status"] == 1
        [event] = configured(receipt, "ConfigurableAccountConfigUpdated")
        assert event == (account.address, account.salt, 0, 2, account.abi_owners)
        assert await CA.fns.getConfigCommitment(account.address).call(w3) == account.commitment
        assert await w3.eth.get_code(account.address) == MARKER

    async def test_a_rotation_hands_the_account_to_the_next_owners(self, w3, chain_id):
        account = await derived(w3, 1, owners(1))
        rotate, after = account.rotate(2, owners(1, 1))
        receipt = await account.send(w3, chain_id, [rotate])
        assert receipt["status"] == 1
        registered, rotated = configured(receipt, "ConfigurableAccountConfigUpdated")
        assert registered == (account.address, account.salt, 0, 1, account.abi_owners)
        assert rotated == (account.address, account.salt, 1, 2, after.abi_owners)
        assert await CA.fns.getConfigCommitment(account.address).call(w3) == after.commitment

        transfer = [transfer_call(new_account().address, 1)]
        with pytest.raises(Web3RPCError, match="account configuration commitment mismatch"):
            await account.send(w3, chain_id, transfer)
        assert (await after.send(w3, chain_id, transfer))["status"] == 1


@pytest.mark.usefixtures("t14")
class TestKeys:
    """A configurable account in the keychain, as a parent and as a key."""

    async def test_its_owners_authorize_an_access_key_for_it(self, w3, chain_id):
        account, key, recipient = await derived(w3, 1, owners(1)), new_account(), new_account().address
        grant = KC.fns.authorizeKey(key.address, SECP256K1, key_restrictions())
        assert (await account.send(w3, chain_id, [grant]))["status"] == 1

        tx = await prepare_tx(w3, chain_id, account, [transfer_call(recipient, 3)])
        signed = sign_tx_registered_key(tx, Signer(key.key.hex()), account.address)
        assert (await send_signed(w3, signed))["status"] == 1
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 3

    async def test_it_signs_as_another_accounts_key(self, w3, chain_id):
        """#8750: the parent names the delegate, and the delegate's quorum signs for the parent."""
        parent, delegate, recipient = await funded(w3), Multisig.derived(2, owners(1, 1)), new_account().address
        await send_call(w3, chain_id, parent, _delegate(delegate.address))

        tx = await prepare_tx(w3, chain_id, parent, [transfer_call(recipient, 4)])
        assert (await send_signed(w3, delegate.sign_as_key(tx, parent.address)))["status"] == 1
        assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 4

    async def test_a_code_free_delegate_is_a_key(self, w3, chain_id):
        root, delegate = await funded(w3), new_account().address
        await send_call(w3, chain_id, root, _delegate(delegate))
        signature_type, key_id, _, _, is_revoked = await KC.fns.getKey(root.address, delegate).call(w3)
        assert (signature_type, key_id.lower(), is_revoked) == (CONFIGURABLE_ACCOUNT, delegate.lower(), False)

    async def test_a_delegate_with_code_or_a_reserved_address_is_refused(self, w3, chain_id):
        """The granting account itself, a contract, a TIP-20 and a precompile are not delegates."""
        root = await funded(w3)
        _, contract = await deploy_contract(w3, chain_id=chain_id, private_key=root.key.hex(), bytecode=RETURN_42_INIT)
        for key_id in (root.address, contract, PATH_USD, ACCOUNT_KEYCHAIN_ADDRESS):
            assert "InvalidKeyId" in await call_revert(w3, _delegate(key_id), sender=root.address), key_id


def _delegate(key_id: str, signature_type: int = CONFIGURABLE_ACCOUNT):
    return KC.fns.authorizeKey(key_id, signature_type, key_restrictions())


class TestBeforeT14:
    async def test_a_delegate_is_refused(self, held_back):
        """The decoder refuses the type before dispatch, as it does any value past WebAuthn."""
        async with rpc(held_back("t14_time")) as w3:
            root, key_id = new_account().address, new_account().address
            plain = {"from": root, "to": ACCOUNT_KEYCHAIN_ADDRESS, "data": _delegate(key_id, SECP256K1).data}
            assert await w3.eth.call(plain) == b""
            assert await call_revert(w3, _delegate(key_id), sender=root) == "execution reverted"

    async def test_there_is_no_account_to_upgrade_into_or_sign_for(self, held_back):
        async with rpc(held_back("t14_time")) as w3:
            chain_id = await w3.eth.chain_id
            assert await w3.eth.get_code(CONFIGURABLE_ACCOUNT_ADDRESS) == b""
            account = await derived(w3, 1, owners(1))
            with pytest.raises(Web3RPCError):
                await account.send(w3, chain_id, [transfer_call(new_account().address, 1)])
