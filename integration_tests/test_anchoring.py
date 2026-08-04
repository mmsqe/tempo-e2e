"""Anchoring precompile (0x…0a00) over JSON-RPC: registries, versioned records, scoped RBAC.

Records are versioned per ``(registryId, checksum)``: ``recordId`` identifies the version
stream, ``index`` the 1-based version, and only the newest carries ``isLatest``. Writes need
``admin`` or ``editor``, scoped to a registry or to a single record stream.
"""

import pytest
from eth_utils import keccak
from hexbytes import HexBytes

from .abi import ANCHORING as A
from .abi import ANCHORING_ADDRESS as ADDR
from .utils import call_revert, fund, new_account, send_call

pytestmark = pytest.mark.tempo  # tempo 0x76 tx, gas in PATH_USD

ADD_REGISTRY_TOPIC = HexBytes(keccak(text="AddRegistry(address,uint64,string)"))
ADD_RECORD_TOPIC = HexBytes(keccak(text="AddRecord(address,uint64,uint64,uint64,string)"))

# Record tuple positions, mirroring the struct field order in abi.py.
URI, CHECKSUM, STATUS, RECORD_ID, INDEX, IS_LATEST, REGISTRY_ID = 0, 1, 5, 6, 7, 8, 9
# Registry tuple positions.
REG_NAME, REG_CREATOR = 1, 3

ADMIN, EDITOR = "admin", "editor"


def page(limit=0):
    return (b"", 0, limit, False, False)


def a_record(registry_id, checksum, uri="ipfs://a", status="active"):
    """A Record tuple; the chain overwrites timestamp/recordId/index/isLatest on write."""
    return (uri, checksum, "sha256", '{"document":"d"}', "", status, 0, 0, False, registry_id)


async def send(w3, chain_id, signer, call):
    return await send_call(w3, chain_id, signer, ADDR, call.data)


async def revert(w3, call, sender):
    return await call_revert(w3, ADDR, call.data, sender=sender)


def registry_id_of(receipt):
    """The `registryId` from the AddRegistry log — the return value is not in the receipt."""
    log = next(lg for lg in receipt["logs"] if lg["topics"][0] == ADD_REGISTRY_TOPIC)
    return int.from_bytes(bytes(log["data"])[:32], "big")


async def new_registry(w3, chain_id, signer, name="docs"):
    return registry_id_of(await send(w3, chain_id, signer, A.fns.addRegistry(name, "", "")))


async def add_record(w3, chain_id, signer, registry_id, checksum, uri="ipfs://a"):
    return await send(w3, chain_id, signer, A.fns.addRecord(a_record(registry_id, checksum, uri)))


async def query_records(w3, registry_id=0, checksum="", record_id=0, index=0, limit=0):
    result = await A.fns.records(registry_id, checksum, record_id, index, page(limit)).call(w3, to=ADDR)
    return result[0]


async def query_registries(w3, registry_id=0, limit=0):
    result = await A.fns.registries(registry_id, page(limit)).call(w3, to=ADDR)
    return result[0]


async def funded(w3):
    account = new_account()
    await fund(w3, account.address)
    return account


@pytest.fixture
async def admin(w3):
    return await funded(w3)


async def test_precompile_is_live_at_genesis(w3):
    """No deployment step: the marker bytecode is allocated at genesis."""
    assert bytes(await w3.eth.get_code(ADDR)) == b"\xef"


async def test_add_registry(w3, chain_id, admin):
    """Permissionless, sequential ids, and registry names need not be unique."""
    first = await new_registry(w3, chain_id, admin)
    second = await new_registry(w3, chain_id, admin)
    assert second == first + 1

    (found,) = await query_registries(w3, registry_id=first)
    assert found[REG_NAME] == "docs"
    assert found[REG_CREATOR].lower() == admin.address.lower()


async def test_add_record_and_query(w3, chain_id, admin):
    """A first record gets recordId 1, index 1, and is the latest version."""
    reg = await new_registry(w3, chain_id, admin)
    await add_record(w3, chain_id, admin, reg, "abc")

    (found,) = await query_records(w3, registry_id=reg, checksum="abc")
    assert found[RECORD_ID] == 1 and found[INDEX] == 1
    assert found[IS_LATEST] is True
    assert found[CHECKSUM] == "abc"


async def test_same_checksum_maintains_record_id_and_versions(w3, chain_id, admin):
    """Re-anchoring a checksum keeps its recordId and bumps index; only the newest is latest."""
    reg = await new_registry(w3, chain_id, admin)
    await add_record(w3, chain_id, admin, reg, "abc", "ipfs://v1")
    await add_record(w3, chain_id, admin, reg, "abc", "ipfs://v2")

    (latest,) = await query_records(w3, registry_id=reg, checksum="abc")
    assert (latest[RECORD_ID], latest[INDEX]) == (1, 2)
    assert latest[IS_LATEST] is True

    (first,) = await query_records(w3, registry_id=reg, record_id=1, index=1)
    assert first[IS_LATEST] is False
    assert first[URI] == "ipfs://v1"


async def test_shared_checksum_across_registries(w3, chain_id, admin):
    """A checksum-only query spans every registry holding it."""
    r1 = await new_registry(w3, chain_id, admin, "a")
    r2 = await new_registry(w3, chain_id, admin, "b")
    await add_record(w3, chain_id, admin, r1, "shared")
    await add_record(w3, chain_id, admin, r2, "shared")

    across = await query_records(w3, checksum="shared")
    assert sorted(r[REGISTRY_ID] for r in across) == [r1, r2]


async def test_grant_and_revoke_role_as_admin(w3, chain_id, admin):
    """Only a registry admin may grant, and a revoked editor loses write access."""
    editor = await funded(w3)
    reg = await new_registry(w3, chain_id, admin)

    # A stranger cannot grant.
    err = await revert(w3, A.fns.grantRole(reg, "", editor.address, EDITOR), sender=editor.address)
    assert "unauthorized" in err

    await send(w3, chain_id, admin, A.fns.grantRole(reg, "", editor.address, EDITOR))
    await add_record(w3, chain_id, editor, reg, "abc")

    await send(w3, chain_id, admin, A.fns.revokeRole(reg, "", editor.address, EDITOR))
    err = await revert(w3, A.fns.addRecord(a_record(reg, "def")), sender=editor.address)
    assert "unauthorized" in err


async def test_disallow_last_admin_self_revoke(w3, chain_id, admin):
    """A registry never reaches zero admins: grant a replacement before stepping down."""
    reg = await new_registry(w3, chain_id, admin)

    err = await revert(w3, A.fns.revokeRole(reg, "", admin.address, ADMIN), sender=admin.address)
    assert "cannot revoke the last registry admin" in err

    second = await funded(w3)
    await send(w3, chain_id, admin, A.fns.grantRole(reg, "", second.address, ADMIN))
    await send(w3, chain_id, second, A.fns.revokeRole(reg, "", admin.address, ADMIN))


async def test_record_level_role_is_per_checksum(w3, chain_id, admin):
    """A record-scoped role authorizes only its own checksum."""
    editor = await funded(w3)
    reg = await new_registry(w3, chain_id, admin)
    await add_record(w3, chain_id, admin, reg, "abc")
    await add_record(w3, chain_id, admin, reg, "def")

    await send(w3, chain_id, admin, A.fns.grantRole(reg, "abc", editor.address, EDITOR))
    await add_record(w3, chain_id, editor, reg, "abc", "ipfs://a2")

    err = await revert(w3, A.fns.addRecord(a_record(reg, "def")), sender=editor.address)
    assert "unauthorized" in err, "a role on one checksum must not authorize another"


async def test_role_scope_existence_validation(w3, chain_id, admin):
    """Roles cannot be granted into a registry or checksum that does not exist."""
    other = new_account()
    ghost = 2**40  # far beyond any registry id this suite can reach
    err = await revert(w3, A.fns.grantRole(ghost, "", other.address, EDITOR), sender=admin.address)
    assert f"registry {ghost} does not exist" in err

    reg = await new_registry(w3, chain_id, admin)
    err = await revert(w3, A.fns.grantRole(reg, "nope", other.address, EDITOR), sender=admin.address)
    assert "does not exist in registry" in err


async def test_update_record_status_is_idempotent(w3, chain_id, admin):
    """updateRecordStatus re-asserting the same status is allowed."""
    reg = await new_registry(w3, chain_id, admin)
    await add_record(w3, chain_id, admin, reg, "abc")

    update = A.fns.updateRecordStatus(reg, 1, 1, "redacted")
    await send(w3, chain_id, admin, update)
    await send(w3, chain_id, admin, update)

    (found,) = await query_records(w3, registry_id=reg, checksum="abc")
    assert found[STATUS] == "redacted"


async def test_queries_respect_limits_and_reject_bad_filters(w3, chain_id, admin):
    """Queries honour `limit`, and reject filter combinations they cannot resolve."""
    reg = await new_registry(w3, chain_id, admin)
    for i in range(3):
        await add_record(w3, chain_id, admin, reg, f"c{i}")

    assert len(await query_records(w3, registry_id=reg, limit=2)) == 2
    assert len(await query_records(w3, registry_id=reg)) == 3

    err = await revert(w3, A.fns.records(0, "", 5, 0, page()), sender=admin.address)
    assert "record_id requires registry_id" in err
    err = await revert(w3, A.fns.records(0, "", 0, 1, page()), sender=admin.address)
    assert "index requires registry_id" in err


async def test_writes_reject_non_eoa_callers(w3):
    """Writes require ``msg.sender == tx.origin``, so a contract cannot reach them.

    An ``eth_call`` from a non-EOA address exercises the same guard.
    """
    # A call whose `from` is the precompile itself can never be the tx origin.
    err = await revert(w3, A.fns.addRegistry("x", "", ""), sender=ADDR)
    assert "sender not an eoa" in err


async def test_events_are_emitted(w3, chain_id, admin):
    """Indexers key off these topics, so they are part of the ABI."""
    receipt = await send(w3, chain_id, admin, A.fns.addRegistry("docs", "", ""))
    log = next(lg for lg in receipt["logs"] if lg["topics"][0] == ADD_REGISTRY_TOPIC)
    assert bytes(log["topics"][1])[-20:].hex() == admin.address[2:].lower()

    receipt = await add_record(w3, chain_id, admin, registry_id_of(receipt), "abc")
    assert any(lg["topics"][0] == ADD_RECORD_TOPIC for lg in receipt["logs"])
