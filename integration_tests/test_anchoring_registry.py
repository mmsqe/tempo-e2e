"""AnchoringRegistry wrapper over JSON-RPC: scoped RBAC, anchoring into the precompile.

The wrapper stores only counters and role membership; everything else is anchored under its
own namespace. Roles are registry- or record-scoped (one checksum in one registry) over
``admin`` and ``editor``, and the owner may grant a registry ``admin`` without holding one.
"""

import json
from pathlib import Path

import pytest
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


async def head(w3, namespace, key) -> bytes:
    """The precompile's latest commitment for ``(namespace, key)``."""
    return bytes(await ANCHORING.fns.latest(namespace, key).call(w3, to=ANCHORING_ADDRESS))


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


async def test_creator_becomes_registry_admin(w3, wrapper):
    creator = await funded(w3)
    rid = await wrapper.add_registry(creator)
    assert await wrapper.read(REG.fns.hasRole(rid, "", creator.address, ADMIN)) is True
    assert await wrapper.read(REG.fns.hasRole(rid, "", wrapper.owner.address, ADMIN)) is False


async def test_writes_require_a_role(w3, wrapper):
    creator, stranger = await funded(w3), await funded(w3)
    rid = await wrapper.add_registry(creator)
    await wrapper.expect_revert(stranger, REG.fns.addRecord(rid, "ipfs://a", "abc", "sha256", "{}"), UNAUTHORIZED)


async def test_grant_and_revoke_registry_editor(w3, wrapper):
    creator, editor = await funded(w3), await funded(w3)
    rid = await wrapper.add_registry(creator)

    # A non-admin cannot grant.
    await wrapper.expect_revert(editor, REG.fns.grantRole(rid, "", editor.address, EDITOR), UNAUTHORIZED)

    await wrapper.write(creator, REG.fns.grantRole(rid, "", editor.address, EDITOR))
    await wrapper.add_record(editor, rid, "abc")

    await wrapper.write(creator, REG.fns.revokeRole(rid, "", editor.address, EDITOR))
    await wrapper.expect_revert(editor, REG.fns.addRecord(rid, "ipfs://a", "def", "sha256", "{}"), UNAUTHORIZED)


async def test_record_role_is_scoped_to_checksum_and_registry(w3, wrapper):
    """A record grant must not leak to another checksum, nor to another registry sharing it."""
    creator, editor = await funded(w3), await funded(w3)
    r1 = await wrapper.add_registry(creator, "a")
    r2 = await wrapper.add_registry(creator, "b")
    await wrapper.add_record(creator, r1, "shared")
    await wrapper.add_record(creator, r1, "other")
    await wrapper.add_record(creator, r2, "shared")

    await wrapper.write(creator, REG.fns.grantRole(r1, "shared", editor.address, EDITOR))

    _, index = await wrapper.add_record(editor, r1, "shared")  # own scope: ok
    assert index == 2
    await wrapper.expect_revert(  # other checksum: no
        editor, REG.fns.addRecord(r1, "ipfs://a", "other", "sha256", "{}"), UNAUTHORIZED
    )
    await wrapper.expect_revert(  # other registry, same checksum: no
        editor, REG.fns.addRecord(r2, "ipfs://a", "shared", "sha256", "{}"), UNAUTHORIZED
    )


async def test_grant_validates_scope_and_role(w3, wrapper):
    creator, other = await funded(w3), new_account()
    await wrapper.expect_revert(creator, REG.fns.grantRole(99, "", other.address, EDITOR), REGISTRY_NOT_FOUND)
    rid = await wrapper.add_registry(creator)
    await wrapper.expect_revert(creator, REG.fns.grantRole(rid, "nope", other.address, EDITOR), NO_RECORD_FOR_CHECKSUM)
    await wrapper.expect_revert(
        creator, REG.fns.grantRole(rid, "", other.address, b"root".ljust(32, b"\x00")), INVALID_ROLE
    )


async def test_last_registry_admin_cannot_be_revoked(w3, wrapper):
    creator, second = await funded(w3), await funded(w3)
    rid = await wrapper.add_registry(creator)

    await wrapper.expect_revert(creator, REG.fns.revokeRole(rid, "", creator.address, ADMIN), LAST_ADMIN)

    # With a replacement in place the original can step down.
    await wrapper.write(creator, REG.fns.grantRole(rid, "", second.address, ADMIN))
    await wrapper.write(second, REG.fns.revokeRole(rid, "", creator.address, ADMIN))
    assert await wrapper.read(REG.fns.hasRole(rid, "", creator.address, ADMIN)) is False


async def test_repeated_grants_do_not_inflate_the_admin_count(w3, wrapper):
    creator, second = await funded(w3), await funded(w3)
    rid = await wrapper.add_registry(creator)
    for _ in range(3):
        await wrapper.write(creator, REG.fns.grantRole(rid, "", second.address, ADMIN))

    await wrapper.write(second, REG.fns.revokeRole(rid, "", creator.address, ADMIN))
    # Were the count inflated, this would still pass; it must hit LastAdmin.
    await wrapper.expect_revert(second, REG.fns.revokeRole(rid, "", second.address, ADMIN), LAST_ADMIN)


async def test_owner_break_glass_grants_registry_admin_only(w3, wrapper):
    """The owner holds no role, yet may install a new admin — and exactly that."""
    creator, rescuer = await funded(w3), await funded(w3)
    rid = await wrapper.add_registry(creator)

    await wrapper.write(wrapper.owner, REG.fns.grantRole(rid, "", rescuer.address, ADMIN))
    assert await wrapper.read(REG.fns.hasRole(rid, "", rescuer.address, ADMIN)) is True

    await wrapper.expect_revert(wrapper.owner, REG.fns.grantRole(rid, "", rescuer.address, EDITOR), UNAUTHORIZED)
    await wrapper.expect_revert(wrapper.owner, REG.fns.revokeRole(rid, "", rescuer.address, ADMIN), UNAUTHORIZED)


async def test_anchors_land_in_the_precompile(w3, wrapper):
    """The wrapper's writes are real anchors: the head is set under its namespace, and a new
    version moves it."""
    creator = await funded(w3)
    rid = await wrapper.add_registry(creator)
    record_id, _ = await wrapper.add_record(creator, rid, "abc")

    key = await wrapper.read(REG.fns.recordKey(rid, record_id))
    before = await head(w3, wrapper.address, key)
    assert before != b"\x00" * 32
    assert bytes(await wrapper.read(REG.fns.latestRecordDigest(rid, record_id))) == before

    await wrapper.add_record(creator, rid, "abc")  # new version moves the head
    assert await head(w3, wrapper.address, key) != before


async def test_update_record_status_is_idempotent_on_chain(w3, wrapper):
    """The envelope's sequence number keeps repeated status writes clear of the no-op rule."""
    creator = await funded(w3)
    rid = await wrapper.add_registry(creator)
    record_id, index = await wrapper.add_record(creator, rid, "abc")

    await wrapper.write(creator, REG.fns.updateRecordStatus(rid, record_id, index, "redacted"))
    await wrapper.write(creator, REG.fns.updateRecordStatus(rid, record_id, index, "redacted"))

    key = await wrapper.read(REG.fns.statusKey(rid, record_id, index))
    assert await head(w3, wrapper.address, key) != b"\x00" * 32
