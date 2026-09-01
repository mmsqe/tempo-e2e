"""Registry contracts over JSON-RPC: scoped RBAC, anchoring into the precompile.

One contract per registry, deployed by a factory; a record is ``keccak256(checksum)``, not an
assigned id. The contract stores only role membership and a version count per record, and anchors
under *its own address*, so the precompile's caller partition is what keeps registries apart --
there is no registryId in any key, mapping or envelope.

Roles are registry- or record-scoped (one checksum) over ``admin`` and ``editor``, and the
owner may grant a registry ``admin`` without holding one. Role changes are not anchored:
membership is contract state, history is the contract's own events.
"""

import pytest
from eth_abi.abi import decode
from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3

from .abi import REGISTRY as REG
from .abi import REGISTRY_FACTORY
from .anchoring import anchored_logs, decode_payload, latest
from .registry import ADMIN, EDITOR, RECORD_CATEGORY, REGISTRY_SCOPE, add_record_call
from .utils import (
    STATE_WRITE_GAS,
    call_forwarder,
    call_revert,
    deploy_contract,
    error_selector,
    funded,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD


UNAUTHORIZED = error_selector("Unauthorized()")
LAST_ADMIN = error_selector("LastAdmin()")
NO_RECORD_FOR_CHECKSUM = error_selector("NoRecordForChecksum(bytes32)")
INVALID_ROLE = error_selector("InvalidRole(bytes32)")
MISSING_ROLE = error_selector("MissingRole(address,bytes32)")
EMPTY_NAME = error_selector("EmptyName()")
EMPTY_CHECKSUM = error_selector("EmptyChecksum()")
CANNOT_RENOUNCE = error_selector("OwnershipCannotBeRenounced()")
NEW_OWNER_IS_ZERO = error_selector("NewOwnerIsZeroAddress()")
EMPTY_URI = error_selector("EmptyUri()")

# Role values the x/anchoring module took and this contract does not: `viewer`, which has no
# successor here because reads need no grant, and an empty role, which meant "editor, then
# admin" to its revoke. Both now name nothing, and naming nothing has to fail rather than
# resolve -- a migrated caller must not quietly grant or revoke `admin`.
RETIRED_ROLES = {"viewer": b"viewer", "empty": b"", "unknown": b"root"}

# The envelopes a registry anchors, field by field. Both lead with their kind, so an indexer
# classifies a payload from the log alone, and the id in both is the checksum hash -- the
# registry is the address it was anchored under, not a field. Both end with the account that
# caused the change, because the precompile's caller is the registry contract, then a
# discriminator keeping two otherwise identical payloads apart.
#
# This is wire format, not an implementation detail, so it is written out rather than read off
# the contract; ``test_the_wire_constants_are_the_contracts_own`` is what ties the two together.
ENVELOPE = {
    "RECORD": {
        "kind": "bytes32",
        "checksum_hash": "bytes32",
        "index": "uint256",
        "uri": "string",
        "checksum": "string",
        "algo": "string",
        "metadata": "string",
        "category": "uint8",
        "data_pointer": "string",
        "author": "address",
        "timestamp": "uint256",
    },
    "STATUS": {
        "kind": "bytes32",
        "checksum_hash": "bytes32",
        "index": "uint256",
        "status": "string",
        "author": "address",
        "seq": "uint256",
    },
}
KINDS = {name: name.lower().encode().ljust(32, b"\x00") for name in ENVELOPE}


def _topic(value) -> HexBytes:
    """One indexed argument as the 32-byte word it is logged as; a ``0x`` string is an address."""
    if isinstance(value, int):
        return HexBytes(value.to_bytes(32, "big"))
    if isinstance(value, str) and value.startswith("0x"):
        return HexBytes(bytes(12) + bytes.fromhex(value[2:]))
    return HexBytes(value)


def assert_event(receipt, emitter, signature: str, *, indexed: list, types: list, data: list):
    topic0 = HexBytes(keccak(text=signature))
    for lg in receipt["logs"]:
        if lg["address"].lower() != emitter.lower():
            continue
        if HexBytes(lg["topics"][0]) != topic0:
            continue
        assert [HexBytes(t) for t in lg["topics"][1:]] == [_topic(v) for v in indexed], signature
        assert list(decode(types, bytes(lg["data"]))) == data, signature
        return
    raise AssertionError(f"{signature} not emitted by {emitter}")


async def envelopes(w3, registry, key, kind, *, from_block=0):
    """Envelopes anchored under ``key``, in log order, as ``(commitment, fields)`` pairs.

    Checks what holds for every kind on the way past: each commitment hashes its own payload,
    and the payload leads with the kind it was keyed under.
    """
    schema = ENVELOPE[kind]
    out = []
    for lg in await anchored_logs(w3, registry.address, key=key, from_block=from_block):
        commitment, payload = decode_payload(lg["data"])
        assert keccak(payload) == commitment, "anchorAndHash makes each event self-verifying"
        fields = dict(zip(schema, decode(list(schema.values()), payload)))
        assert fields["kind"] == KINDS[kind], f"envelope under {HexBytes(key).hex()} is not a {kind}"
        out.append((commitment, fields))
    return out


class TestDeployment:
    async def test_creator_becomes_registry_admin(self, factory, registry):
        creator = registry.creator
        assert await registry.has_role(creator, ADMIN) is True
        assert await registry.has_role(factory.owner, ADMIN) is False

    async def test_names_may_repeat_and_the_address_is_canonical(self, w3, factory):
        creator, other = await funded(w3), await funded(w3)
        a = await factory.deploy(creator, "docs")
        b = await factory.deploy(other, "docs")
        assert a.address != b.address
        assert await a.has_role(creator, ADMIN) is True
        assert await b.has_role(creator, ADMIN) is False, "admin does not cross registries"

    async def test_deployment_event_records_the_creation(self, w3, factory):
        """A registry's name, description and metadata live only in this event.

        They are not anchored -- descriptive, set once, nothing to prove -- so the deployment
        log is the whole on-chain record of what was created and by whom.
        """
        creator = await funded(w3)
        registry = await factory.deploy(creator, "docs", "the docs", '{"src":"e2e"}')
        assert_event(
            registry.deployment,
            factory.address,
            "RegistryDeployed(address,address,string,string,string)",
            indexed=[registry.address, creator.address],
            types=["string", "string", "string"],
            data=["docs", "the docs", '{"src":"e2e"}'],
        )

    async def test_an_empty_name_is_rejected(self, w3, factory):
        creator = await funded(w3)
        err = await call_revert(
            w3,
            factory.address,
            REGISTRY_FACTORY.fns.deployRegistry("", "", "").data,
            sender=creator.address,
        )
        assert EMPTY_NAME in err, err

    async def test_registries_are_separate_namespaces_in_the_log(self, w3, factory):
        """The whole design in one assertion: two registries, the same key, their own heads."""
        creator = await funded(w3)
        a = await factory.deploy(creator, "a")
        b = await factory.deploy(creator, "b")
        # The same checksum in both, so the same key -- it derives from the checksum and
        # nothing else. Differing uris keep the payloads, and so the heads, apart.
        await a.add_record(creator, "shared", uri="ipfs://in-a")
        await b.add_record(creator, "shared", uri="ipfs://in-b")

        key = await a.read(REG.fns.recordKey(keccak(text="shared")))
        assert bytes(key) == bytes(await b.read(REG.fns.recordKey(keccak(text="shared")))), "the same key..."
        head_a = await latest(w3, a.address, key)
        head_b = await latest(w3, b.address, key)
        assert head_a != b"\x00" * 32 and head_b != b"\x00" * 32
        assert head_a != head_b, "...each holding its own head, because the caller partitions"


class TestRecords:
    async def test_writes_require_a_role(self, w3, registry):
        stranger = await funded(w3)
        await registry.expect_revert(stranger, add_record_call("abc"), UNAUTHORIZED)

    async def test_a_record_needs_both_a_checksum_and_a_uri(self, registry):
        """The checksum *is* the record's identity and an empty uri anchors a version pointing
        at nothing, so neither is something the contract could supply. Refused before the role
        check, so an empty checksum is not a way to probe whether a stream exists either.
        """
        creator = registry.creator

        await registry.expect_revert(creator, add_record_call(""), EMPTY_CHECKSUM)
        await registry.expect_revert(creator, add_record_call("abc", uri=""), EMPTY_URI)

        assert await registry.versions("abc") == 0, "neither started a stream"

    async def test_a_category_past_the_enum_is_rejected(self, w3, registry):
        """The vendored enum is the suite's category mapping, so it has to be the real one.

        The ABI carries `RecordCategory` as a bare uint8, so nothing about the names is
        checkable from the ABI alone. What the chain does answer is the boundary: the enum's
        last member is accepted and one past it is not, which pins the vendored list's length
        to the deployed contract rather than to whenever it was last copied.
        """
        creator, last = registry.creator, len(RECORD_CATEGORY) - 1
        assert RECORD_CATEGORY["unspecified"] == 0, "a record claiming no category says so"

        await registry.add_record(creator, "abc", category=last)

        # Identical bar the category, so the revert can only be the category.
        err = await call_revert(
            w3, registry.address, add_record_call("def", category=last + 1).data, sender=creator.address
        )
        assert err, "a category past the enum must revert"

    async def test_update_record_status_is_idempotent_on_chain(self, w3, registry):
        """The envelope's sequence number keeps repeated status writes clear of the no-op rule.

        A moved head would also pass if the second write were dropped, so the test reads ``seq``
        out of both envelopes.
        """
        creator, checksum_hash = registry.creator, keccak(text="abc")
        await registry.add_record(creator, "abc")

        await registry.set_status(creator, "abc", 1, "redacted")
        await registry.set_status(creator, "abc", 1, "redacted")

        key = await registry.read(REG.fns.statusKey(checksum_hash, 1))
        assert await latest(w3, registry.address, key) != b"\x00" * 32

        anchored = [f for _, f in await envelopes(w3, registry, key, "STATUS")]
        assert len(anchored) == 2, "the identical repeat anchors again rather than reverting"
        for fields in anchored:
            assert (fields["checksum_hash"], fields["index"], fields["status"]) == (checksum_hash, 1, "redacted")
        assert anchored[1]["seq"] > anchored[0]["seq"], "seq is what makes the payloads differ"


class TestRoles:
    async def test_grant_and_revoke_registry_editor(self, w3, registry):
        creator, editor = registry.creator, await funded(w3)

        # A non-admin cannot grant.
        await registry.expect_revert(editor, REG.fns.grantRole("", editor.address, EDITOR), UNAUTHORIZED)

        await registry.grant(creator, editor, EDITOR)
        await registry.add_record(editor, "abc")

        await registry.revoke(creator, editor, EDITOR)
        await registry.expect_revert(editor, add_record_call("def"), UNAUTHORIZED)

    async def test_record_role_is_scoped_to_checksum_and_registry(self, w3, factory):
        """A record grant must not leak to another checksum, nor to another registry sharing it.

        The second half is now the address doing the scoping rather than an id in the role key.
        """
        creator, editor = await funded(w3), await funded(w3)
        a = await factory.deploy(creator, "a")
        b = await factory.deploy(creator, "b")
        await a.add_record(creator, "shared")
        await a.add_record(creator, "other")
        await b.add_record(creator, "shared")

        await a.grant(creator, editor, EDITOR, checksum="shared")

        await a.add_record(editor, "shared")  # own scope: ok
        assert await a.versions("shared") == 2
        await a.expect_revert(editor, add_record_call("other"), UNAUTHORIZED)  # other checksum: no
        await b.expect_revert(editor, add_record_call("shared"), UNAUTHORIZED)  # other registry: no

    async def test_the_two_scopes_are_a_union_not_an_override(self, w3, registry):
        """``_checkWriter`` is an OR, so the module's "record level overrides registry level"
        has no successor: a record-scoped grant adds a writer to one stream and takes nothing
        from a registry-scoped one. There is no way to deny.
        """
        creator, wide, narrow = registry.creator, await funded(w3), await funded(w3)
        await registry.add_record(creator, "abc")
        await registry.grant(creator, wide, EDITOR)
        await registry.grant(creator, narrow, EDITOR, checksum="abc")

        await registry.add_record(narrow, "abc")
        await registry.add_record(wide, "abc")  # still writes the narrowed stream
        await registry.add_record(wide, "def")  # and every other one

        assert await registry.versions("abc") == 3
        assert await registry.versions("def") == 1

    async def test_granting_on_a_checksum_with_no_record_is_rejected(self, registry):
        """A record scope is a stream that exists; naming one that does not is a typo, and a
        role granted under it would sit there authorizing nothing."""
        creator, other = registry.creator, new_account()

        await registry.expect_revert(creator, REG.fns.grantRole("nope", other.address, EDITOR), NO_RECORD_FOR_CHECKSUM)

    @pytest.mark.parametrize("role", list(RETIRED_ROLES), ids=list(RETIRED_ROLES))
    async def test_a_role_this_contract_does_not_know_is_rejected(self, registry, role):
        """Both directions: a role that cannot be granted must not look revocable either, and
        ``revokeRole`` validates before it looks for a membership -- so an empty role fails
        there rather than resolving the way the module's did."""
        creator, other = registry.creator, new_account()
        value = RETIRED_ROLES[role].ljust(32, b"\x00")

        await registry.expect_revert(creator, REG.fns.grantRole("", other.address, value), INVALID_ROLE)
        await registry.expect_revert(creator, REG.fns.revokeRole("", other.address, value), INVALID_ROLE)

    async def test_only_an_admin_may_revoke(self, w3, registry):
        """Revoking is admin-only: holding a role is not enough to shed it."""
        creator, editor = registry.creator, await funded(w3)
        await registry.grant(creator, editor, EDITOR)

        # Not even on itself.
        await registry.expect_revert(editor, REG.fns.revokeRole("", editor.address, EDITOR), UNAUTHORIZED)
        assert await registry.has_role(editor, EDITOR) is True

        await registry.revoke(creator, editor, EDITOR)
        assert await registry.has_role(editor, EDITOR) is False

    async def test_revoking_a_role_never_held_reverts(self, registry):
        """A revoke names a specific grant, so a wrong one fails rather than no-opping."""
        creator, stranger = registry.creator, new_account()

        await registry.expect_revert(creator, REG.fns.revokeRole("", stranger.address, EDITOR), MISSING_ROLE)

    async def test_roles_are_independent_across_accounts(self, w3, registry):
        """Grants are per account: revoking one leaves the others holding theirs."""
        creator, first, second = registry.creator, await funded(w3), await funded(w3)

        for account in (first, second):
            await registry.grant(creator, account, EDITOR)
        await registry.grant(creator, second, ADMIN)

        await registry.revoke(creator, first, EDITOR)

        assert await registry.has_role(first, EDITOR) is False
        assert await registry.has_role(second, EDITOR) is True
        assert await registry.has_role(second, ADMIN) is True
        # The surviving editor grant still authorizes a write.
        await registry.add_record(second, "abc")

    async def test_last_registry_admin_cannot_be_revoked(self, w3, registry):
        creator, second = registry.creator, await funded(w3)

        await registry.expect_revert(creator, REG.fns.revokeRole("", creator.address, ADMIN), LAST_ADMIN)

        # With a replacement in place the original can step down.
        await registry.grant(creator, second, ADMIN)
        await registry.revoke(second, creator, ADMIN)
        assert await registry.has_role(creator, ADMIN) is False

    async def test_repeated_grants_do_not_inflate_the_admin_count(self, w3, registry):
        creator, second = registry.creator, await funded(w3)
        for _ in range(3):
            await registry.grant(creator, second, ADMIN)

        await registry.revoke(second, creator, ADMIN)
        # Were the count inflated, this would still pass; it must hit LastAdmin.
        await registry.expect_revert(second, REG.fns.revokeRole("", second.address, ADMIN), LAST_ADMIN)

    async def test_the_factory_cannot_be_left_without_an_owner(self, w3, factory):
        """The last-admin rule holds because break-glass does, so the rescuer cannot go away.
        Both ways out are closed: `Ownable` refuses the zero address, this refuses to renounce.
        """

        async def refused(fn) -> str:
            return await call_revert(w3, factory.address, fn.data, sender=factory.owner.address)

        assert CANNOT_RENOUNCE in await refused(REGISTRY_FACTORY.fns.renounceOwnership())
        zero = REGISTRY_FACTORY.fns.transferOwnership("0x" + "00" * 20)
        assert NEW_OWNER_IS_ZERO in await refused(zero)

    async def test_owner_break_glass_grants_registry_admin_only(self, factory, w3, registry):
        """The owner holds no role, yet may install a new admin — and exactly that."""
        rescuer = await funded(w3)
        assert Web3.to_checksum_address(await registry.read(REG.fns.owner())) == (factory.owner.address), (
            "the factory hands its owner to every registry"
        )

        await registry.grant(factory.owner, rescuer, ADMIN)
        assert await registry.has_role(rescuer, ADMIN) is True

        await registry.expect_revert(factory.owner, REG.fns.grantRole("", rescuer.address, EDITOR), UNAUTHORIZED)
        await registry.expect_revert(factory.owner, REG.fns.revokeRole("", rescuer.address, ADMIN), UNAUTHORIZED)


class TestAnchoredLog:
    async def test_anchors_land_in_the_precompile(self, w3, registry):
        """A registry's writes are real anchors: a new version moves the head."""
        creator, checksum_hash = registry.creator, keccak(text="abc")
        await registry.add_record(creator, "abc")

        key = await registry.read(REG.fns.recordKey(checksum_hash))
        before = await latest(w3, registry.address, key)
        assert before != b"\x00" * 32
        assert bytes(await registry.read(REG.fns.latestRecordDigest(checksum_hash))) == before

        await registry.add_record(creator, "abc")  # new version moves the head
        assert await latest(w3, registry.address, key) != before

    async def test_the_anchored_log_alone_reconstructs_a_record_stream(self, w3, registry):
        """Version history is only in the log, since the contract keeps no record data.

        Each envelope leads with its kind, so the key it was anchored under confirms the shape
        rather than being the only thing that identifies it.
        """
        creator, checksum_hash = registry.creator, keccak(text="abc")
        await registry.add_record(creator, "abc", uri="ipfs://v1")
        await registry.add_record(
            creator,
            "abc",
            uri="ipfs://v2",
            category=RECORD_CATEGORY["regulated_bank_underwriting"],
            data_pointer="loan-42",
        )
        await registry.add_record(creator, "def", uri="ipfs://other")

        key = await registry.read(REG.fns.recordKey(checksum_hash))

        versions, commitments, classified = {}, {}, {}
        for commitment, fields in await envelopes(w3, registry, key, "RECORD"):
            index = fields["index"]
            versions[index] = (fields["uri"], fields["checksum"])
            commitments[index] = commitment
            classified[index] = (fields["category"], fields["data_pointer"], Web3.to_checksum_address(fields["author"]))

        assert versions == {1: ("ipfs://v1", "abc"), 2: ("ipfs://v2", "abc")}
        # Category, pointer and author survive into the envelope, per version — the only place
        # they exist, since the contract stores no record data.
        assert classified == {
            1: (RECORD_CATEGORY["unspecified"], "", creator.address),
            2: (RECORD_CATEGORY["regulated_bank_underwriting"], "loan-42", creator.address),
        }
        newest = max(versions)
        assert newest == await registry.versions("abc")
        digest = await registry.read(REG.fns.latestRecordDigest(checksum_hash))
        assert bytes(digest) == commitments[newest]

    async def test_acl_changes_are_not_anchored(self, w3, registry):
        """Role changes reach the log as events only.

        Membership is this contract's state and its history is RoleGranted/RoleRevoked, which
        carry every field. A third copy in the anchored log would only be something to drift —
        so a grant and a revoke must move the state while anchoring nothing at all.
        """
        creator, editor = registry.creator, await funded(w3)

        before = await w3.eth.block_number
        receipts = [
            await registry.grant(creator, editor, EDITOR),
            await registry.revoke(creator, editor, EDITOR),
        ]

        anchored = await anchored_logs(w3, registry.address, from_block=before + 1)
        assert anchored == [], f"a role change reached the anchored log: {anchored}"
        assert await registry.has_role(editor, EDITOR) is False, "...but the state moved"
        for receipt in receipts:
            assert any(lg["address"].lower() == registry.address.lower() for lg in receipt["logs"]), (
                "the registry still emits its own role event"
            )

    async def test_the_wire_constants_are_the_contracts_own(self, registry):
        """Every literal this suite writes down, read back off the contract.

        The kind tags and the role values are wire format an indexer matches on, and
        ``REGISTRY_SCOPE`` is what an empty checksum resolves to. Reading both sides is what
        stops the two drifting apart.
        """
        for name, value in [
            *((f"KIND_{kind}", tag) for kind, tag in KINDS.items()),
            ("ROLE_ADMIN", ADMIN),
            ("ROLE_EDITOR", EDITOR),
            ("REGISTRY_SCOPE", REGISTRY_SCOPE),
        ]:
            on_chain = await registry.read(getattr(REG.fns, name)())
            assert bytes(on_chain) == value, f"{name} moved away from {value!r}"


class TestContractAccounts:
    """Who a role can be held by, and how much fits in one transaction.

    The module refused contract callers outright -- ``sender not an eoa`` -- so a multisig had
    to be a Cosmos one signing module messages. Here a role is held by an address and a
    contract is an address; and a tempo tx carries several calls, which is what the module's
    multi-message tx was for.
    """

    async def test_a_contract_can_hold_a_role_and_write_with_it(self, w3, chain_id, registry):
        creator = registry.creator
        _, safe = await deploy_contract(
            w3, chain_id=chain_id, private_key=creator.key.hex(), bytecode=call_forwarder(registry.address)
        )
        write = add_record_call("abc").data

        # Being a contract grants it nothing; the grant does.
        assert UNAUTHORIZED in await call_revert(w3, registry.address, write, sender=safe)

        await registry.write(creator, REG.fns.grantRole("", safe, EDITOR))
        assert await registry.read(REG.fns.hasRole("", safe, EDITOR)) is True

        # The EOA drives the contract; the contract is what the registry sees as its writer.
        await send_call(w3, chain_id, creator, safe, write)

        assert await registry.versions("abc") == 1
        key = await registry.read(REG.fns.recordKey(keccak(text="abc")))
        assert await latest(w3, registry.address, key) != b"\x00" * 32, (
            "and the anchor is still the registry's, not the contract's that called it"
        )
        assert await latest(w3, safe, key) == b"\x00" * 32

    async def test_one_transaction_carries_a_whole_change_or_none_of_it(self, w3, chain_id, registry):
        """A grant, a record and its status in one tx -- and a call that reverts takes the
        others with it, which is what made the module's multi-message tx worth using."""
        creator, editor = registry.creator, await funded(w3)
        calls = [
            {"to": registry.address, "data": REG.fns.grantRole("", editor.address, EDITOR).data},
            {"to": registry.address, "data": add_record_call("abc").data},
            {"to": registry.address, "data": REG.fns.updateRecordStatus("abc", 1, "approved").data},
        ]

        receipt = await send_calls(
            w3, chain_id=chain_id, private_key=creator.key.hex(), calls=calls, gas_limit=STATE_WRITE_GAS
        )
        assert receipt["status"] == 1
        assert await registry.has_role(editor, EDITOR) is True
        assert await registry.versions("abc") == 1

        second = await funded(w3)
        doomed = [
            {"to": registry.address, "data": REG.fns.grantRole("", second.address, EDITOR).data},
            # No version 9 of that record, so this one reverts.
            {"to": registry.address, "data": REG.fns.updateRecordStatus("abc", 9, "approved").data},
        ]
        receipt = await send_calls(
            w3, chain_id=chain_id, private_key=creator.key.hex(), calls=doomed, gas_limit=STATE_WRITE_GAS
        )

        assert receipt["status"] == 0
        assert await registry.has_role(second, EDITOR) is False, "the grant before it did not land"


class TestEvents:
    async def test_record_events_carry_their_identifiers(self, registry):
        """RecordAdded and RecordStatusUpdated name what they changed."""
        creator = registry.creator

        # The author is indexed, so a consumer deduplicating an operator's attestations filters
        # on it rather than reading every record in the registry.
        receipt = await registry.add_record(
            creator, "abc", category=RECORD_CATEGORY["agentic_ai"], data_pointer="did:x#1"
        )
        checksum_hash = keccak(text="abc")
        assert_event(
            receipt,
            registry.address,
            "RecordAdded(bytes32,uint256,string,uint8,string,address)",
            indexed=[checksum_hash, creator.address],
            types=["uint256", "string", "uint8", "string"],
            data=[1, "abc", RECORD_CATEGORY["agentic_ai"], "did:x#1"],
        )

        receipt = await registry.set_status(creator, "abc", 1, "redacted")
        assert_event(
            receipt,
            registry.address,
            "RecordStatusUpdated(bytes32,uint256,string)",
            indexed=[checksum_hash],
            types=["uint256", "string"],
            data=[1, "redacted"],
        )

    async def test_role_events_carry_the_grant(self, w3, registry):
        """The event names the scope in readable form; nothing else records it."""
        creator, editor = registry.creator, await funded(w3)
        await registry.add_record(creator, "abc")

        receipt = await registry.grant(creator, editor, EDITOR, checksum="abc")
        assert_event(
            receipt,
            registry.address,
            "RoleGranted(bytes32,address,bytes32)",
            indexed=[HexBytes(keccak(text="abc")), editor.address],
            types=["bytes32"],
            data=[EDITOR],
        )

        receipt = await registry.revoke(creator, editor, EDITOR, checksum="abc")
        assert_event(
            receipt,
            registry.address,
            "RoleRevoked(bytes32,address,bytes32)",
            indexed=[HexBytes(keccak(text="abc")), editor.address],
            types=["bytes32"],
            data=[EDITOR],
        )
