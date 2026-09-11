"""The anchoring contract at 0x…0a00, on a node built like the network's genesis: code in the alloc,
the seed fixture loaded at block 0."""

import time

import pytest
from eth_account import Account
from eth_utils import filter_abi_by_type, function_abi_to_4byte_selector, keccak
from tempo import Signer, add_fee_payer_signature, serialize, sign_transaction
from tempo.keychain import sign_tx_access_key

from .abi import ANCHORING, ANCHORING_ADDRESS
from .anchoring import (
    GO_TIME,
    RUNTIME_CODE,
    Page,
    Record,
    Registry,
    add_record,
    anchoring_node,
    bech32,
    emitted,
    new_registry,
    records,
    registries,
    registries_by_name,
)
from .utils import (
    DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
    RETURN_42_INIT,
    STATE_WRITE_GAS,
    build_tempo_tx,
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
    suggested_max_fee,
)

pytestmark = pytest.mark.tempo


@pytest.fixture(scope="module")
def module_admin():
    return new_account()


@pytest.fixture(scope="module")
def tempo(tmp_path_factory, module_admin):
    """This module's node, in place of the session's."""
    with anchoring_node(tmp_path_factory.mktemp("anchoring"), module_admin.address) as node:
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
        assert bytes(await w3.eth.get_code(ANCHORING_ADDRESS)) == RUNTIME_CODE
        schedule = (await w3.provider.make_request("tempo_forkSchedule", []))["result"]["schedule"]
        assert next(fork for fork in schedule if fork["name"] == "T10")["active"]
        assert [r.id for r in await registries(w3, 1)] == [1]


class TestSeededCorpus:
    """SeedFixture.t.sol's corpus and writes on top of it; none touch registries 1 and 2."""

    ALICE = "0x00000000000000000000000000000000000A11cE"
    BOB = "0x0000000000000000000000000000000000000B0b"
    AT = "2025-09-09 00:00:00 +0000 UTC"
    CITATION = "1 C.C.A. 144"

    REGISTRIES = [
        Registry(1, "us-ca1", "First Circuit", bech32(ALICE), AT, '{"tranche":1}'),
        Registry(2, "us-ca9", "Ninth Circuit", bech32(BOB), AT, "{}"),
        Registry(3, "us-ca1", "", bech32(ALICE), AT, ""),
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
        assert [r.id for r in await registries_by_name(w3, "us-ca1", Page(limit=2))] == [1, 3]
        # Roles are keccak of the strings the keeper formats; a record role hex-encodes both parts.
        admin, editor = keccak(text="registry:1:admin"), keccak(text="registry:1:editor")
        record_admin = keccak(text=f"record:1:{self.CITATION.encode().hex()}:{b'admin'.hex()}")
        assert await ANCHORING.fns.hasRole(admin, self.ALICE).call(w3)
        assert await ANCHORING.fns.roleMemberCount(admin).call(w3) == 1
        assert await ANCHORING.fns.hasRole(editor, self.BOB).call(w3)
        assert await ANCHORING.fns.hasRole(record_admin, self.BOB).call(w3)

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

    async def test_the_module_admin_recovers_registry_3(self, w3, chain_id, module_admin):
        """Registry 3's only admin has no key; the module admin, and nobody else, can appoint one."""
        await fund(w3, module_admin.address)
        successor = await funded(w3)
        grant = ANCHORING.fns.grantRole(3, "", successor.address, "admin").data
        assert "missing required role" in await call_revert(w3, ANCHORING_ADDRESS, grant, sender=successor.address)

        await send_call(w3, chain_id, module_admin, ANCHORING_ADDRESS, grant)
        assert await ANCHORING.fns.roleMemberCount(keccak(text="registry:3:admin")).call(w3) == 2
        receipt = await send_call(w3, chain_id, successor, ANCHORING_ADDRESS, add_record(3, "sha:r"))
        assert emitted(receipt, "AddRecord") == [(successor.address, 3, 1, 1, "sha:r")]


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


class TestEoaGate:
    """Each way Tempo sends reaches the contract as the signer; a contract in between is refused."""

    # Relays its calldata to the anchoring contract and returns the answer, revert data included:
    # calldatacopy, call, returndatacopy, then revert or (at 0x33) return.
    RELAY_RUNTIME = (
        "36 6000 6000 37  6000 6000 36 6000 6000 73{addr} 5a f1  3d 6000 6000 3e  6033 57  3d 6000 fd  5b 3d 6000 f3"
    )

    @staticmethod
    async def assert_created_by(w3, receipt, account: str) -> None:
        """``receipt`` created a registry with ``account`` as its caller, creator and admin."""
        assert receipt["status"] == 1
        [(caller, registry_id, _)] = emitted(receipt, "AddRegistry")
        assert caller == account
        [registry] = await registries(w3, registry_id)
        assert registry.creator == bech32(account)
        assert await ANCHORING.fns.hasRole(keccak(text=f"registry:{registry_id}:admin"), account).call(w3)

    async def test_type_2(self, w3, chain_id, funded_account):
        """What the old chain's integrations send."""
        tx = {
            "to": ANCHORING_ADDRESS,
            "data": ANCHORING.fns.addRegistry("type-2", "", "").data,
            "value": 0,
            "nonce": await w3.eth.get_transaction_count(funded_account.address),
            "chainId": chain_id,
            "gas": STATE_WRITE_GAS,
            "maxFeePerGas": await suggested_max_fee(w3),
            "maxPriorityFeePerGas": DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
            "type": 2,
        }
        raw = Account.sign_transaction(tx, funded_account.key).raw_transaction
        receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw))
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
        tx = build_tempo_tx(
            chain_id=chain_id,
            nonce=await get_nonce(w3, sender.address),
            gas_limit=STATE_WRITE_GAS,
            max_fee_per_gas=await suggested_max_fee(w3),
            calls=[{"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.addRegistry("sponsored", "", "").data}],
            awaiting_fee_payer=True,
        )
        signed = add_fee_payer_signature(sign_transaction(tx, Signer(sender.key.hex())), Signer(payer.key.hex()))
        receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(serialize(signed)))
        await self.assert_created_by(w3, receipt, sender.address)

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
        """Its 23 bytes of code are the one kind the gate admits."""
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

    async def test_a_contract_is_refused(self, w3, chain_id, funded_account):
        runtime = bytes.fromhex(self.RELAY_RUNTIME.format(addr=ANCHORING_ADDRESS[2:]))
        size = f"60{len(runtime):02x}"
        init = bytes.fromhex(f"{size} 600c 6000 39 {size} 6000 f3") + runtime  # copy the runtime out, return it
        key = funded_account.key.hex()
        _, relay = await deploy_contract(w3, chain_id=chain_id, private_key=key, bytecode=init)
        data = ANCHORING.fns.addRegistry("relayed", "", "").data
        assert "sender not an eoa" in await call_revert(w3, relay, data, sender=funded_account.address)

        [last] = await registries(w3, page=Page(limit=1, reverse=True))
        calls = [{"to": relay, "data": data}]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=key, calls=calls, gas_limit=STATE_WRITE_GAS)
        assert receipt["status"] == 0
        assert await registries(w3, page=Page(limit=1, reverse=True)) == [last], "nothing was written"
