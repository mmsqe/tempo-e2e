"""AnchoringRegistry wrapper over JSON-RPC: scoped RBAC, anchoring into the precompile.

The wrapper stores only counters and role membership; everything else is anchored under its
own namespace. Roles are registry- or record-scoped (one checksum in one registry) over
``admin`` and ``editor``, and the owner may grant a registry ``admin`` without holding one.
"""

import json
from pathlib import Path

import pytest
from eth_abi.abi import decode
from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3

from .abi import ANCHORING, ANCHORING_ADDRESS, ANCHORING_DEPLOYER
from .abi import ANCHORING_REGISTRY as REG
from .utils import call_revert, error_selector, fund, new_account, send_call, send_calls

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

ADMIN = b"admin".ljust(32, b"\x00")
EDITOR = b"editor".ljust(32, b"\x00")

_ARTIFACT = json.loads((Path(__file__).parent / "artifacts" / "anchoring.json").read_text())


UNAUTHORIZED = error_selector("Unauthorized()")  # solady Ownable's error, reused by the wrapper
LAST_ADMIN = error_selector("LastAdmin()")
REGISTRY_NOT_FOUND = error_selector("RegistryNotFound(uint256)")
NO_RECORD_FOR_CHECKSUM = error_selector("NoRecordForChecksum(uint256,bytes32)")
INVALID_ROLE = error_selector("InvalidRole(bytes32)")
MISSING_ROLE = error_selector("MissingRole(address,bytes32)")

ANCHORED_TOPIC = HexBytes(keccak(text="Anchored(address,bytes32,bytes32,bytes)"))
# Every envelope leads with its kind, so an indexer classifies a payload from the log alone.
# Spelled out because these literals are wire format, not an implementation detail;
# ``TestAnchoredLog.test_kind_tags_are_the_wire_constants`` ties them to the contract's own.
KINDS = {name: name.lower().encode().ljust(32, b"\x00") for name in ("REGISTRY", "RECORD", "STATUS", "ACL")}
# The envelopes the wrapper anchors, in field order, each after its leading kind.
REGISTRY_ENVELOPE = ["bytes32", "uint256", "string", "string", "string", "address", "uint256"]
RECORD_ENVELOPE = ["bytes32", "uint256", "uint256", "uint256", "string", "string", "string", "string", "uint256"]
STATUS_ENVELOPE = ["bytes32", "uint256", "uint256", "uint256", "string", "uint256"]
# One grant's new state, keyed by aclKey(registryId, checksumHash, account, role).
ACL_ENVELOPE = ["bytes32", "uint256", "bytes32", "address", "bytes32", "bool"]


def _topic(value) -> HexBytes:
    if isinstance(value, int):
        return HexBytes(value.to_bytes(32, "big"))
    if isinstance(value, str) and value.startswith("0x"):
        return HexBytes(bytes(12) + bytes.fromhex(value[2:]))
    return HexBytes(value)


def assert_event(receipt, wrapper, signature: str, *, indexed: list, types: list, data: list):
    topic0 = HexBytes(keccak(text=signature))
    for lg in receipt["logs"]:
        if lg["address"].lower() != wrapper.address.lower():
            continue
        if HexBytes(lg["topics"][0]) != topic0:
            continue
        assert [HexBytes(t) for t in lg["topics"][1:]] == [_topic(v) for v in indexed], signature
        assert list(decode(types, bytes(lg["data"]))) == data, signature
        return
    raise AssertionError(f"{signature} not emitted by {wrapper.address}")


async def anchored_logs(w3, wrapper, *, key=None, from_block=0):
    topics = [ANCHORED_TOPIC, HexBytes(bytes(12) + bytes.fromhex(wrapper.address[2:]))]
    if key is not None:
        topics.append(HexBytes(key))
    return await w3.eth.get_logs({"fromBlock": from_block, "address": ANCHORING_ADDRESS, "topics": topics})


async def head(w3, namespace, key) -> bytes:
    """The precompile's latest commitment for ``(namespace, key)``."""
    return bytes(await ANCHORING.fns.latest(namespace, key).call(w3, to=ANCHORING_ADDRESS))


async def envelopes(w3, wrapper, key, types, kind, *, from_block=0):
    """Envelopes anchored under ``key``, in log order, as ``(commitment, fields)`` pairs.

    Checks what holds for every kind on the way past: each commitment hashes its own payload,
    and the payload leads with the kind it was keyed under.
    """
    out = []
    for lg in await anchored_logs(w3, wrapper, key=key, from_block=from_block):
        commitment, envelope = decode(["bytes32", "bytes"], bytes(lg["data"]))
        assert keccak(envelope) == commitment, "anchorAndHash makes each event self-verifying"
        fields = decode(types, envelope)
        assert fields[0] == KINDS[kind], f"envelope under {HexBytes(key).hex()} is not a {kind}"
        out.append((commitment, fields))
    return out


class Wrapper:
    """A deployed registry bound to its node + address, wrapping call boilerplate."""

    def __init__(self, w3, chain_id, address, owner):
        self.w3, self.chain_id, self.address, self.owner = w3, chain_id, address, owner

    async def read(self, fn):
        return await fn.call(self.w3, to=self.address)

    async def write(self, signer, fn):
        return await send_call(self.w3, self.chain_id, signer, self.address, fn.data)

    async def expect_revert(self, sender, fn, selector):
        err = await call_revert(self.w3, self.address, fn.data, sender=sender.address)
        assert selector in err, err

    async def grant(self, signer, registry_id, account, role, checksum=""):
        return await self.write(signer, REG.fns.grantRole(registry_id, checksum, account.address, role))

    async def revoke(self, signer, registry_id, account, role, checksum=""):
        return await self.write(signer, REG.fns.revokeRole(registry_id, checksum, account.address, role))

    async def has_role(self, registry_id, account, role, checksum="") -> bool:
        return await self.read(REG.fns.hasRole(registry_id, checksum, account.address, role))

    async def add_registry(self, signer, name="docs"):
        await self.write(signer, REG.fns.addRegistry(name, "", ""))
        return await self.read(REG.fns.registryCount())

    async def add_record(self, signer, registry_id, checksum, uri="ipfs://a"):
        await self.write(signer, REG.fns.addRecord(registry_id, uri, checksum, "sha256", "{}"))
        record_id = await self.read(REG.fns.recordIdForChecksum(registry_id, checksum))
        return record_id, await self.read(REG.fns.versionCount(registry_id, record_id))


@pytest.fixture
async def wrapper(w3, chain_id):
    """A fresh wrapper per test; the deploying EOA becomes owner (break-glass admin)."""
    owner = new_account()
    await fund(w3, owner.address)
    initcode = bytes.fromhex(_ARTIFACT["deployer_bytecode"][2:])
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=owner.key.hex(),
        calls=[{"to": None, "data": initcode}],
        gas_limit=25_000_000,  # deploys two contracts (impl + proxy); far above the 2M default
    )
    assert receipt["status"] == 1, "deployer create reverted"
    deployer = receipt["contractAddress"]
    proxy = await ANCHORING_DEPLOYER.fns.registry().call(w3, to=deployer)
    return Wrapper(w3, chain_id, Web3.to_checksum_address(proxy), owner)


async def funded(w3):
    account = new_account()
    await fund(w3, account.address)
    return account


class TestRecords:
    async def test_creator_becomes_registry_admin(self, w3, wrapper):
        creator = await funded(w3)
        rid = await wrapper.add_registry(creator)
        assert await wrapper.has_role(rid, creator, ADMIN) is True
        assert await wrapper.has_role(rid, wrapper.owner, ADMIN) is False

    async def test_writes_require_a_role(self, w3, wrapper):
        creator, stranger = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)
        await wrapper.expect_revert(stranger, REG.fns.addRecord(rid, "ipfs://a", "abc", "sha256", "{}"), UNAUTHORIZED)

    async def test_update_record_status_is_idempotent_on_chain(self, w3, wrapper):
        """The envelope's sequence number keeps repeated status writes clear of the no-op rule.

        A moved head would also pass if the second write were dropped, so the test reads ``seq``
        out of both envelopes.
        """
        creator = await funded(w3)
        rid = await wrapper.add_registry(creator)
        record_id, index = await wrapper.add_record(creator, rid, "abc")

        await wrapper.write(creator, REG.fns.updateRecordStatus(rid, record_id, index, "redacted"))
        await wrapper.write(creator, REG.fns.updateRecordStatus(rid, record_id, index, "redacted"))

        key = await wrapper.read(REG.fns.statusKey(rid, record_id, index))
        assert await head(w3, wrapper.address, key) != b"\x00" * 32

        anchored = [f for _, f in await envelopes(w3, wrapper, key, STATUS_ENVELOPE, "STATUS")]
        assert len(anchored) == 2, "the identical repeat anchors again rather than reverting"
        for _, registry_id, rec, idx, status, _ in anchored:
            assert (registry_id, rec, idx, status) == (rid, record_id, index, "redacted")
        assert anchored[1][-1] > anchored[0][-1], "seq is what makes the payloads differ"


class TestRoles:
    async def test_grant_and_revoke_registry_editor(self, w3, wrapper):
        creator, editor = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)

        # A non-admin cannot grant.
        await wrapper.expect_revert(editor, REG.fns.grantRole(rid, "", editor.address, EDITOR), UNAUTHORIZED)

        await wrapper.grant(creator, rid, editor, EDITOR)
        await wrapper.add_record(editor, rid, "abc")

        await wrapper.revoke(creator, rid, editor, EDITOR)
        await wrapper.expect_revert(editor, REG.fns.addRecord(rid, "ipfs://a", "def", "sha256", "{}"), UNAUTHORIZED)

    async def test_record_role_is_scoped_to_checksum_and_registry(self, w3, wrapper):
        """A record grant must not leak to another checksum, nor to another registry sharing it."""
        creator, editor = await funded(w3), await funded(w3)
        r1 = await wrapper.add_registry(creator, "a")
        r2 = await wrapper.add_registry(creator, "b")
        await wrapper.add_record(creator, r1, "shared")
        await wrapper.add_record(creator, r1, "other")
        await wrapper.add_record(creator, r2, "shared")

        await wrapper.grant(creator, r1, editor, EDITOR, checksum="shared")

        _, index = await wrapper.add_record(editor, r1, "shared")  # own scope: ok
        assert index == 2
        await wrapper.expect_revert(  # other checksum: no
            editor, REG.fns.addRecord(r1, "ipfs://a", "other", "sha256", "{}"), UNAUTHORIZED
        )
        await wrapper.expect_revert(  # other registry, same checksum: no
            editor, REG.fns.addRecord(r2, "ipfs://a", "shared", "sha256", "{}"), UNAUTHORIZED
        )

    async def test_grant_validates_scope_and_role(self, w3, wrapper):
        creator, other = await funded(w3), new_account()
        await wrapper.expect_revert(creator, REG.fns.grantRole(99, "", other.address, EDITOR), REGISTRY_NOT_FOUND)
        rid = await wrapper.add_registry(creator)
        await wrapper.expect_revert(
            creator, REG.fns.grantRole(rid, "nope", other.address, EDITOR), NO_RECORD_FOR_CHECKSUM
        )
        await wrapper.expect_revert(
            creator, REG.fns.grantRole(rid, "", other.address, b"root".ljust(32, b"\x00")), INVALID_ROLE
        )

    async def test_only_an_admin_may_revoke(self, w3, wrapper):
        """Revoking is admin-only: holding a role is not enough to shed it."""
        creator, editor = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)
        await wrapper.grant(creator, rid, editor, EDITOR)

        # Not even on itself.
        await wrapper.expect_revert(editor, REG.fns.revokeRole(rid, "", editor.address, EDITOR), UNAUTHORIZED)
        assert await wrapper.has_role(rid, editor, EDITOR) is True

        await wrapper.revoke(creator, rid, editor, EDITOR)
        assert await wrapper.has_role(rid, editor, EDITOR) is False

    async def test_revoking_a_role_never_held_reverts(self, w3, wrapper):
        """A revoke names a specific grant, so a wrong one fails rather than no-opping."""
        creator, stranger = await funded(w3), new_account()
        rid = await wrapper.add_registry(creator)

        await wrapper.expect_revert(creator, REG.fns.revokeRole(rid, "", stranger.address, EDITOR), MISSING_ROLE)

    async def test_roles_are_independent_across_accounts(self, w3, wrapper):
        """Grants are per account: revoking one leaves the others holding theirs."""
        creator, first, second = await funded(w3), await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)

        for account in (first, second):
            await wrapper.grant(creator, rid, account, EDITOR)
        await wrapper.grant(creator, rid, second, ADMIN)

        await wrapper.revoke(creator, rid, first, EDITOR)

        assert await wrapper.has_role(rid, first, EDITOR) is False
        assert await wrapper.has_role(rid, second, EDITOR) is True
        assert await wrapper.has_role(rid, second, ADMIN) is True
        # The surviving editor grant still authorizes a write.
        await wrapper.add_record(second, rid, "abc")

    async def test_last_registry_admin_cannot_be_revoked(self, w3, wrapper):
        creator, second = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)

        await wrapper.expect_revert(creator, REG.fns.revokeRole(rid, "", creator.address, ADMIN), LAST_ADMIN)

        # With a replacement in place the original can step down.
        await wrapper.grant(creator, rid, second, ADMIN)
        await wrapper.revoke(second, rid, creator, ADMIN)
        assert await wrapper.has_role(rid, creator, ADMIN) is False

    async def test_repeated_grants_do_not_inflate_the_admin_count(self, w3, wrapper):
        creator, second = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)
        for _ in range(3):
            await wrapper.grant(creator, rid, second, ADMIN)

        await wrapper.revoke(second, rid, creator, ADMIN)
        # Were the count inflated, this would still pass; it must hit LastAdmin.
        await wrapper.expect_revert(second, REG.fns.revokeRole(rid, "", second.address, ADMIN), LAST_ADMIN)

    async def test_owner_break_glass_grants_registry_admin_only(self, w3, wrapper):
        """The owner holds no role, yet may install a new admin — and exactly that."""
        creator, rescuer = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)

        await wrapper.grant(wrapper.owner, rid, rescuer, ADMIN)
        assert await wrapper.has_role(rid, rescuer, ADMIN) is True

        await wrapper.expect_revert(wrapper.owner, REG.fns.grantRole(rid, "", rescuer.address, EDITOR), UNAUTHORIZED)
        await wrapper.expect_revert(wrapper.owner, REG.fns.revokeRole(rid, "", rescuer.address, ADMIN), UNAUTHORIZED)


class TestAnchoredLog:
    async def test_anchors_land_in_the_precompile(self, w3, wrapper):
        """The wrapper's writes are real anchors: a new version moves the head."""
        creator = await funded(w3)
        rid = await wrapper.add_registry(creator)
        record_id, _ = await wrapper.add_record(creator, rid, "abc")

        key = await wrapper.read(REG.fns.recordKey(rid, record_id))
        before = await head(w3, wrapper.address, key)
        assert before != b"\x00" * 32
        assert bytes(await wrapper.read(REG.fns.latestRecordDigest(rid, record_id))) == before

        await wrapper.add_record(creator, rid, "abc")  # new version moves the head
        assert await head(w3, wrapper.address, key) != before

    async def test_the_anchored_log_alone_reconstructs_a_record_stream(self, w3, wrapper):
        """Version history is only in the log, since the wrapper keeps no record data.

        Each envelope leads with its kind, so the key it was anchored under confirms the shape
        rather than being the only thing that identifies it.
        """
        creator = await funded(w3)
        rid = await wrapper.add_registry(creator, "docs")
        await wrapper.add_record(creator, rid, "abc", uri="ipfs://v1")
        record_id, _ = await wrapper.add_record(creator, rid, "abc", uri="ipfs://v2")
        await wrapper.add_record(creator, rid, "def", uri="ipfs://other")

        key = await wrapper.read(REG.fns.recordKey(rid, record_id))

        versions, commitments = {}, {}
        for commitment, (_, _, _, index, uri, checksum, *_) in await envelopes(
            w3, wrapper, key, RECORD_ENVELOPE, "RECORD"
        ):
            versions[index] = (uri, checksum)
            commitments[index] = commitment

        assert versions == {1: ("ipfs://v1", "abc"), 2: ("ipfs://v2", "abc")}
        latest = max(versions)
        assert latest == await wrapper.read(REG.fns.versionCount(rid, record_id))
        digest = await wrapper.read(REG.fns.latestRecordDigest(rid, record_id))
        assert bytes(digest) == commitments[latest]

    async def test_acl_changes_are_anchored(self, w3, wrapper):
        """Permissions rebuild from the log too: every real grant or revoke anchors its state.

        A repeated grant stays a no-op, so granted and revoked alternate and no sequence number
        is needed to clear the precompile's ``CommitmentUnchanged`` rule.
        """
        creator, editor = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)
        checksum_hash = keccak(text="")  # registry scope
        key = await wrapper.read(REG.fns.aclKey(rid, checksum_hash, editor.address, EDITOR))

        before = await w3.eth.block_number
        receipts = [
            await wrapper.grant(creator, rid, editor, EDITOR),
            await wrapper.grant(creator, rid, editor, EDITOR),  # already a member: anchors nothing
            await wrapper.revoke(creator, rid, editor, EDITOR),
        ]

        states = []
        for _, (_, registry_id, scope, account, role, granted) in await envelopes(
            w3, wrapper, key, ACL_ENVELOPE, "ACL", from_block=before + 1
        ):
            assert (registry_id, scope, role) == (rid, checksum_hash, EDITOR)
            assert Web3.to_checksum_address(account) == editor.address
            states.append(granted)

        assert states == [True, False], "one anchor per real change; the repeated grant is a no-op"
        assert await head(w3, wrapper.address, key) != b"\x00" * 32
        for receipt in receipts:
            assert any(lg["address"].lower() == wrapper.address.lower() for lg in receipt["logs"]), (
                "the wrapper still emits its own role event"
            )

    async def test_the_registry_envelope_records_its_creation(self, w3, wrapper):
        """A registry's name, description, metadata and creator live only in its envelope.

        The wrapper keeps a counter and nothing else, so this payload is the only on-chain
        record of what was created and by whom.
        """
        creator = await funded(w3)
        await wrapper.write(creator, REG.fns.addRegistry("docs", "the docs", '{"src":"e2e"}'))
        rid = await wrapper.read(REG.fns.registryCount())
        key = await wrapper.read(REG.fns.registryKey(rid))

        anchored = await envelopes(w3, wrapper, key, REGISTRY_ENVELOPE, "REGISTRY")
        assert len(anchored) == 1, "a registry is created once and never re-anchored"
        _, (_, id_, name, description, metadata, account, timestamp) = anchored[0]
        assert (id_, name, description, metadata) == (rid, "docs", "the docs", '{"src":"e2e"}')
        assert Web3.to_checksum_address(account) == creator.address
        assert timestamp > 0, "the envelope carries the block time, since the log does not repeat it"

    async def test_kind_tags_are_the_wire_constants(self, wrapper):
        """The tags indexers match on are the constants the contract exposes.

        Reading both sides is what stops the wire format and the contract drifting apart.
        """
        for name, tag in KINDS.items():
            on_chain = await wrapper.read(getattr(REG.fns, f"KIND_{name}")())
            assert bytes(on_chain) == tag, f"KIND_{name} moved away from {name.lower()!r}"


class TestEvents:
    async def test_record_events_carry_their_identifiers(self, w3, wrapper):
        """RegistryAdded, RecordAdded and RecordStatusUpdated name what they changed."""
        creator = await funded(w3)

        receipt = await wrapper.write(creator, REG.fns.addRegistry("docs", "", ""))
        rid = await wrapper.read(REG.fns.registryCount())
        assert_event(
            receipt,
            wrapper,
            "RegistryAdded(uint256,string,address)",
            indexed=[rid, creator.address],
            types=["string"],
            data=["docs"],
        )

        receipt = await wrapper.write(creator, REG.fns.addRecord(rid, "ipfs://a", "abc", "sha256", "{}"))
        record_id = await wrapper.read(REG.fns.recordIdForChecksum(rid, "abc"))
        assert_event(
            receipt,
            wrapper,
            "RecordAdded(uint256,uint256,uint256,string)",
            indexed=[rid, record_id],
            types=["uint256", "string"],
            data=[1, "abc"],
        )

        receipt = await wrapper.write(creator, REG.fns.updateRecordStatus(rid, record_id, 1, "redacted"))
        assert_event(
            receipt,
            wrapper,
            "RecordStatusUpdated(uint256,uint256,uint256,string)",
            indexed=[rid, record_id],
            types=["uint256", "string"],
            data=[1, "redacted"],
        )

    async def test_role_events_carry_the_grant(self, w3, wrapper):
        """The event names the scope in readable form; the anchored envelope hashes it."""
        creator, editor = await funded(w3), await funded(w3)
        rid = await wrapper.add_registry(creator)
        await wrapper.add_record(creator, rid, "abc")

        receipt = await wrapper.grant(creator, rid, editor, EDITOR, checksum="abc")
        assert_event(
            receipt,
            wrapper,
            "RoleGranted(uint256,bytes32,address,bytes32)",
            indexed=[rid, editor.address],
            types=["bytes32", "bytes32"],
            data=[keccak(text="abc"), EDITOR],
        )

        receipt = await wrapper.revoke(creator, rid, editor, EDITOR, checksum="abc")
        assert_event(
            receipt,
            wrapper,
            "RoleRevoked(uint256,bytes32,address,bytes32)",
            indexed=[rid, editor.address],
            types=["bytes32", "bytes32"],
            data=[keccak(text="abc"), EDITOR],
        )
