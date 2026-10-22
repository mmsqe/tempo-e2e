"""The anchoring contract at 0x…0a00, on a node built like the network's genesis: code in the alloc,
the seed fixture loaded at block 0."""

import time

import pytest
from eth_utils import filter_abi_by_type, function_abi_to_4byte_selector, keccak, to_checksum_address
from tempo import Signer
from tempo.keychain import sign_tx_access_key

from .abi import ANCHORING, ANCHORING_ADDRESS
from .anchoring import (
    GO_TIME,
    PROXY_CODE,
    Page,
    Record,
    Registry,
    add_record,
    anchoring_node,
    bech32,
    deploy_relay,
    emitted,
    new_registry,
    records,
    registries,
    registries_by_name,
)
from .network import ExternalNode
from .utils import (
    RETURN_42_INIT,
    STATE_WRITE_GAS,
    ZERO_ADDRESS,
    active_forks,
    call_revert,
    deploy_contract,
    fund,
    funded,
    get_nonce,
    new_account,
    prepare_tx,
    send_call,
    send_calls,
    send_set_code_tx,
    send_signed,
    send_type_2,
    sign_sponsored,
)

pytestmark = [pytest.mark.tempo, pytest.mark.anchoring]

OWNER = new_account()  # named in this module's genesis


def intrinsic(data: bytes) -> int:
    """What a transaction owes before it runs anything: 21k plus its calldata."""
    return 21_000 + sum(4 if byte == 0 else 16 for byte in bytes(data))


@pytest.fixture(scope="module")
def tempo(request, tmp_path_factory):
    """This module's node, unless ``--tempo-rpc`` names one, such as a node carrying the real dump."""
    rpc_url = request.config.getoption("--tempo-rpc")
    if rpc_url:
        yield ExternalNode(rpc_url, request.config.getoption("--tempo-ws")).wait_for_rpc()
        return
    with anchoring_node(tmp_path_factory.mktemp("anchoring"), owner=OWNER.address) as node:
        yield node


class TestBinding:
    """The precompile's selectors and topics, as go-abi generated them into anchoring.abi.go."""

    SELECTORS = {
        "addRegistry": "318b38b1",
        "addRecord": "64d25295",
        "updateRecordStatus": "97b40c25",
        "records": "c7be5e37",
        "registries": "17bd3e65",
        "registriesByName": "5522e6c6",
        "grantRole": "b8fdd1a7",
        "revokeRole": "acd58bc7",
    }
    TOPICS = {
        "AddRegistry": "181791bc379acedd3615cf065d3c275dfa6a3c4614c9065d54c98773f576108d",
        "AddRecord": "1a3295fa8cc0e28c95d21912c9e6958f3bc740231781f7640ad885c972a352fd",
        "UpdateRecordStatus": "d7b75457d41293eab4829975c951ce8c53106866f0c429d175fc6c91cdad5ade",
        "GrantRole": "0f49e365baf90deb7d1f63e576637907e12d1ddc75d1ac68894a2bcd192b6ddb",
        "RevokeRole": "8236b76cce80eaf69b54d89268d00fda3dec9e5e054f1548ebbb1f8b20b3b08b",
    }

    def test_selectors_and_topics(self):
        functions = filter_abi_by_type("function", ANCHORING.abi)
        selectors = {abi["name"]: function_abi_to_4byte_selector(abi).hex() for abi in functions}
        assert {name: selectors.get(name) for name in self.SELECTORS} == self.SELECTORS
        assert {name: getattr(ANCHORING.events, name).topic.hex() for name in self.TOPICS} == self.TOPICS

    async def test_the_code_answers_under_t10(self, w3):
        """T10 once put a precompile here, and a precompile answers before code."""
        assert bytes(await w3.eth.get_code(ANCHORING_ADDRESS)) == PROXY_CODE
        assert "T10" in await active_forks(w3)
        assert [r.id for r in await registries(w3, 1)] == [1]


class TestSeededCorpus:
    """SeedFixture.t.sol's corpus and writes on top of it; none touch registries 1 and 2."""

    @pytest.fixture(autouse=True)
    def _needs_the_seeded_corpus(self, request):
        """Only this module's own node carries this corpus; any other holds its own."""
        if request.config.getoption("--tempo-rpc"):
            pytest.skip("attached to another node, which carries a corpus of its own")

    ALICE = "0x00000000000000000000000000000000000A11cE"
    BOB = "0x0000000000000000000000000000000000000B0b"
    AT = "2025-09-09 00:00:00 +0000 UTC"
    CITATION = "1 C.C.A. 144"

    REGISTRIES = [
        Registry(1, "us-ca1", "First Circuit", bech32(ALICE), AT, '{"tranche":1}'),
        Registry(2, "us-ca9", "Ninth Circuit", bech32(BOB), AT, "{}"),
        Registry(3, "US-CA1", "", bech32(ALICE), AT, ""),
    ]
    # CITATION twice in registry 1 and once in registry 2; the rest of each record is shared.
    ALIKE = ("cite-canonical-v1", '{"cluster":8857414,"name":"Richmond v. Atwood"}', AT, "Active")
    FIRST = Record("https://www.courtlistener.com/opinion/8857414/x/", CITATION, *ALIKE, 1, 1, False, 1)
    SECOND = FIRST._replace(uri="https://www.courtlistener.com/opinion/8857414/y/", index=2, is_latest=True)
    OTHER = SECOND._replace(uri="https://ex.test/0123456789abcdef", checksum="2 C.C.A. 7", record_id=2, index=1)
    ELSEWHERE = SECOND._replace(uri="https://ex.test/o", index=1, registry_id=2)

    async def test_registries(self, w3):
        assert await registries(w3, page=Page(limit=3)) == self.REGISTRIES
        assert await registries(w3, 2) == self.REGISTRIES[1:2]
        _, (next_key, _) = await ANCHORING.fns.registries(0, Page(limit=2)).call(w3)
        assert next_key == (3).to_bytes(8, "big"), "the next id, as CollectionPaginate encodes it"
        assert await registries(w3, page=Page(key=next_key, limit=1)) == self.REGISTRIES[2:]

    async def test_records(self, w3):
        assert await records(w3, 1, self.CITATION, index=1) == [self.FIRST]
        assert await records(w3, 1, record_id=1, index=1) == [self.FIRST]
        assert await records(w3, 1, self.CITATION) == [self.SECOND]
        assert await records(w3, 1) == [self.SECOND, self.OTHER]
        assert await records(w3, checksum=self.CITATION, page=Page(limit=2)) == [self.SECOND, self.ELSEWHERE]
        assert await records(w3, page=Page(limit=3)) == [self.SECOND, self.OTHER, self.ELSEWHERE]

    async def test_names_and_roles(self, w3):
        assert [r.id for r in await registries_by_name(w3, "us-ca9")] == [2]
        # Registry 3 is seeded as US-CA1, so the dump's index has to be keyed folded.
        for spelling in ("us-ca1", "US-CA1", "Us-Ca1"):
            assert [r.id for r in await registries_by_name(w3, spelling, Page(limit=2))] == [1, 3], spelling
        # Roles are keccak of the strings the keeper formats; a record role hex-encodes both parts.
        admin, editor = keccak(text="registry:1:admin"), keccak(text="registry:1:editor")
        record_admin = keccak(text=f"record:1:{self.CITATION.encode().hex()}:{b'admin'.hex()}")
        assert await ANCHORING.fns.hasRole(admin, self.ALICE).call(w3)
        assert await ANCHORING.fns.roleMemberCount(admin).call(w3) == 1
        assert await ANCHORING.fns.hasRole(editor, self.BOB).call(w3)
        assert await ANCHORING.fns.hasRole(record_admin, self.BOB).call(w3)

    async def test_only_ascii_folds(self, w3, chain_id, funded_account):
        """The fold is A-Z only, so a capital from another script stays as written."""
        registry_id = await new_registry(w3, chain_id, funded_account, "Ünicode-Fold")
        assert [r.id for r in await registries_by_name(w3, "ÜNICODE-FOLD")] == [registry_id]
        assert await registries_by_name(w3, "ünicode-fold") == []

    async def test_a_new_registry_takes_the_next_id(self, w3, chain_id, funded_account):
        me = funded_account.address
        [last] = await registries(w3, page=Page(limit=1, reverse=True))
        new_id = last.id + 1
        data = ANCHORING.fns.addRegistry("us-ca1", "Again", "{}").data
        receipt = await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, data)

        assert emitted(receipt, "AddRegistry") == [(me, new_id, "us-ca1")]
        at = time.strftime(GO_TIME, time.gmtime((await w3.eth.get_block(receipt["blockNumber"]))["timestamp"]))
        assert await registries(w3, new_id) == [Registry(new_id, "us-ca1", "Again", bech32(me), at, "{}")]
        ids = [r.id for r in await registries_by_name(w3, "us-ca1")]
        assert ids[:2] == [1, 3] and ids[-1] == new_id

    async def test_a_seeded_checksum_in_a_new_registry(self, w3, chain_id, funded_account):
        registry_id = await new_registry(w3, chain_id, funded_account)
        data = add_record(registry_id, self.CITATION)
        receipt = await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, data)

        assert emitted(receipt, "AddRecord") == [(funded_account.address, registry_id, 1, 1, self.CITATION)]
        across = await records(w3, checksum=self.CITATION)
        assert across[:2] == [self.SECOND, self.ELSEWHERE]
        assert across[-1].registry_id == registry_id

    async def test_registry_3_is_frozen_to_everyone_but_the_owner(self, w3):
        """Registry 3's only admin has no key, so no key can appoint another."""
        successor = await funded(w3)
        grant = ANCHORING.fns.grantRole(3, "", successor.address, "admin").data
        assert "missing required role" in await call_revert(w3, ANCHORING_ADDRESS, grant, sender=successor.address)
        assert await ANCHORING.fns.roleMemberCount(keccak(text="registry:3:admin")).call(w3) == 1


class TestWrites:
    async def test_a_version_and_a_status(self, w3, chain_id, funded_account):
        me = funded_account.address
        registry_id = await new_registry(w3, chain_id, funded_account)
        v1, v2 = (
            add_record(registry_id, "sha:v", "https://ex.test/v1"),
            add_record(registry_id, "sha:v", "https://ex.test/v2"),
        )
        await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, v1)
        second = await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, v2)
        assert emitted(second, "AddRecord") == [(me, registry_id, 1, 2, "sha:v")]

        status = ANCHORING.fns.updateRecordStatus(registry_id, 1, 1, "Superseded").data
        updated = await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, status)
        assert emitted(updated, "UpdateRecordStatus") == [(me, registry_id, 1, 1, "Superseded")]

        [first] = await records(w3, registry_id, "sha:v", index=1)
        [latest] = await records(w3, registry_id, "sha:v")
        assert (first.status, first.is_latest, latest.index, latest.status) == ("Superseded", False, 2, "Active")
        assert latest.timestamp == time.strftime(
            GO_TIME, time.gmtime((await w3.eth.get_block(second["blockNumber"]))["timestamp"])
        )

    async def test_roles(self, w3, chain_id, funded_account):
        admin, editor = funded_account, await funded(w3)
        registry_id = await new_registry(w3, chain_id, admin)
        grant = ANCHORING.fns.grantRole(registry_id, "", editor.address, "editor").data
        revoke = ANCHORING.fns.revokeRole(registry_id, "", editor.address, "editor").data
        change = (admin.address, registry_id, "", editor.address, "editor")
        first, second = add_record(registry_id, "sha:1"), add_record(registry_id, "sha:2")

        assert "unauthorized" in await call_revert(w3, ANCHORING_ADDRESS, first, sender=editor.address)
        assert emitted(await send_call(w3, chain_id, admin, ANCHORING_ADDRESS, grant), "GrantRole") == [change]
        await send_call(w3, chain_id, editor, ANCHORING_ADDRESS, first)
        assert emitted(await send_call(w3, chain_id, admin, ANCHORING_ADDRESS, revoke), "RevokeRole") == [change]
        assert "unauthorized" in await call_revert(w3, ANCHORING_ADDRESS, second, sender=editor.address)

        last_admin = ANCHORING.fns.revokeRole(registry_id, "", admin.address, "admin").data
        refused = await call_revert(w3, ANCHORING_ADDRESS, last_admin, sender=admin.address)
        assert "cannot revoke the last registry admin" in refused


class TestGas:
    """What the node charges: forge prices its own VM, and an estimate is not a receipt."""

    async def test_an_estimate_covers_the_write(self, w3, funded_account):
        data = ANCHORING.fns.addRegistry("estimate", "covers the receipt", "{}").data
        await send_type_2(w3, funded_account, ANCHORING_ADDRESS, data)  # at the estimate, which must not run out

    async def test_every_write_costs_more_than_its_calldata(self, w3, chain_id, funded_account):
        """Each method runs: one that did nothing would stop at the intrinsic cost."""
        editor = new_account().address
        registry_id = await new_registry(w3, chain_id, funded_account, name="gas-matrix")
        await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, add_record(registry_id, "sha:gas"))
        [record] = await records(w3, registry_id, "sha:gas")
        status = ANCHORING.fns.updateRecordStatus(registry_id, record.record_id, record.index, "Superseded")

        writes = {
            "addRegistry": ANCHORING.fns.addRegistry("gas-matrix-2", "", "{}").data,
            "addRecord": add_record(registry_id, "sha:gas-2"),
            "updateRecordStatus": status.data,
            "grantRole": ANCHORING.fns.grantRole(registry_id, "", editor, "editor").data,
            "revokeRole": ANCHORING.fns.revokeRole(registry_id, "", editor, "editor").data,
        }

        for method, data in writes.items():
            receipt = await send_type_2(w3, funded_account, ANCHORING_ADDRESS, data)
            assert receipt["gasUsed"] > intrinsic(data), method

    async def test_a_bigger_metadata_costs_more(self, w3, funded_account):
        """Past the calldata it adds: the string lands in storage, a slot at a time."""
        costs = {}
        for size in (100, 1000):
            data = ANCHORING.fns.addRegistry(f"gas-scale-{size}", "", "m" * size).data
            receipt = await send_type_2(w3, funded_account, ANCHORING_ADDRESS, data)
            costs[size] = receipt["gasUsed"] - intrinsic(data)

        assert costs[1000] > costs[100]


class TestFieldLimits:
    """The caps in the module's record validation, at the byte either side."""

    async def test_a_checksum_algo_over_its_cap_is_refused(self, w3, chain_id, funded_account):
        """MAX_CHECKSUM_ALGO is 128 bytes."""
        registry_id = await new_registry(w3, chain_id, funded_account, name="limits")

        at_cap = add_record(registry_id, "sha:at-cap", algo="a" * 128)
        await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, at_cap)

        over = add_record(registry_id, "sha:over-cap", algo="a" * 129)
        refused = await call_revert(w3, ANCHORING_ADDRESS, over, sender=funded_account.address)
        assert "checksum algorithm exceeds max length" in refused


class TestCaller:
    """Each way Tempo sends reaches the contract as the signer, and a contract reaches it as itself."""

    @staticmethod
    async def assert_created_by(w3, receipt, account: str) -> None:
        """``receipt`` created a registry with ``account`` as its caller, creator and admin."""
        assert receipt["status"] == 1
        [(caller, registry_id, _)] = emitted(receipt, "AddRegistry")
        assert caller == account
        [registry] = await registries(w3, registry_id)
        assert registry.creator == bech32(account)
        assert await ANCHORING.fns.hasRole(keccak(text=f"registry:{registry_id}:admin"), account).call(w3)

    async def test_type_2(self, w3, funded_account):
        """What the old chain's integrations send."""
        data = ANCHORING.fns.addRegistry("type-2", "", "").data
        receipt = await send_type_2(w3, funded_account, ANCHORING_ADDRESS, data, gas=STATE_WRITE_GAS)
        assert receipt["type"] == 2
        await self.assert_created_by(w3, receipt, funded_account.address)

    async def test_a_tempo_batch(self, w3, chain_id, funded_account):
        [last] = await registries(w3, page=Page(limit=1, reverse=True))
        calls = [
            {"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.addRegistry("batched", "", "").data},
            {"to": ANCHORING_ADDRESS, "data": add_record(last.id + 1, "sha:b")},
        ]
        receipt = await send_calls(
            w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=calls, gas_limit=STATE_WRITE_GAS
        )
        await self.assert_created_by(w3, receipt, funded_account.address)
        assert emitted(receipt, "AddRecord") == [(funded_account.address, last.id + 1, 1, 1, "sha:b")]

    async def test_a_fee_payer(self, w3, chain_id):
        """The sender holds nothing and is still the caller."""
        sender, payer = new_account(), await funded(w3)
        calls = [{"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.addRegistry("sponsored", "", "").data}]
        signed = await sign_sponsored(w3, chain_id, sender, payer, calls)
        await self.assert_created_by(w3, await send_signed(w3, signed), sender.address)

    async def test_an_access_key(self, w3, chain_id):
        root, key = await funded(w3), new_account()
        tx = await prepare_tx(
            w3,
            chain_id,
            root,
            [{"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.addRegistry("access-key", "", "").data}],
        )
        receipt = await send_signed(w3, sign_tx_access_key(tx, key.key.hex(), Signer(root.key.hex()), is_admin=True))
        await self.assert_created_by(w3, receipt, root.address)

    async def test_a_7702_delegation(self, w3, chain_id, funded_account):
        """Its 23 bytes of code do not make it a contract."""
        authority = await funded(w3)
        _, delegate = await deploy_contract(
            w3, chain_id=chain_id, private_key=funded_account.key.hex(), bytecode=RETURN_42_INIT
        )
        await send_set_code_tx(
            w3,
            chain_id=chain_id,
            sponsor=funded_account,
            authority=authority,
            delegate=delegate,
            auth_nonce=await get_nonce(w3, authority.address),
            to=funded_account.address,
        )
        assert len(await w3.eth.get_code(authority.address)) == 23
        data = ANCHORING.fns.addRegistry("delegated", "", "").data
        await self.assert_created_by(
            w3, await send_call(w3, chain_id, authority, ANCHORING_ADDRESS, data), authority.address
        )

    async def test_a_contract_anchors_as_itself(self, w3, chain_id, funded_account):
        relay = await deploy_relay(w3, chain_id, funded_account)
        data = ANCHORING.fns.addRegistry("relayed", "", "").data
        receipt = await send_call(w3, chain_id, funded_account, relay, data)
        await self.assert_created_by(w3, receipt, relay)
        [(_, registry_id, _)] = emitted(receipt, "AddRegistry")
        assert not await ANCHORING.fns.hasRole(
            keccak(text=f"registry:{registry_id}:admin"), funded_account.address
        ).call(w3)


class TestOwner:
    """The module's admin, named in the genesis."""

    @pytest.fixture(autouse=True)
    async def owner_funded(self, request, w3):
        if request.config.getoption("--tempo-rpc"):
            pytest.skip("an external node names its own owner")
        await fund(w3, OWNER.address)

    async def test_the_owner_recovers_registry_3(self, w3, chain_id):
        assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == OWNER.address
        role = keccak(text="registry:3:admin")

        successor = await funded(w3)
        grant = ANCHORING.fns.grantRole(3, "", successor.address, "admin").data
        receipt = await send_call(w3, chain_id, OWNER, ANCHORING_ADDRESS, grant)
        assert emitted(receipt, "GrantRole") == [(OWNER.address, 3, "", successor.address, "admin")]
        assert await ANCHORING.fns.roleMemberCount(role).call(w3) == 2
        receipt = await send_call(w3, chain_id, successor, ANCHORING_ADDRESS, add_record(3, "sha:recovered"))
        assert emitted(receipt, "AddRecord") == [(successor.address, 3, 1, 1, "sha:recovered")]

        # The break-glass is a registry admin and nothing else.
        editor = ANCHORING.fns.grantRole(3, "", successor.address, "editor").data
        assert "missing required role" in await call_revert(w3, ANCHORING_ADDRESS, editor, sender=OWNER.address)

    async def test_the_ownership_moves_to_a_contract(self, w3, chain_id, funded_account):
        relay = await deploy_relay(w3, chain_id, funded_account)
        zero = ANCHORING.fns.transferOwnership(ZERO_ADDRESS).data
        assert "new owner is the zero address" in await call_revert(w3, ANCHORING_ADDRESS, zero, sender=OWNER.address)
        handover = ANCHORING.fns.transferOwnership(relay).data
        receipt = await send_call(w3, chain_id, OWNER, ANCHORING_ADDRESS, handover)
        assert emitted(receipt, "OwnershipTransferred") == [(OWNER.address, relay)]
        assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == relay

        grant = ANCHORING.fns.grantRole(2, "", funded_account.address, "admin").data
        assert "missing required role" in await call_revert(w3, ANCHORING_ADDRESS, grant, sender=OWNER.address)
        receipt = await send_call(w3, chain_id, funded_account, relay, grant)
        assert emitted(receipt, "GrantRole") == [(relay, 2, "", funded_account.address, "admin")]

        # And back, for the rest of the module.
        back = ANCHORING.fns.transferOwnership(OWNER.address).data
        receipt = await send_call(w3, chain_id, funded_account, relay, back)
        assert emitted(receipt, "OwnershipTransferred") == [(relay, OWNER.address)]
