"""registries with checksum records, versioning, and RBAC, over JSON-RPC

The bundled AnchoringDeployer stands up impl + ERC-1967 proxy in one create tx, granting deploying EOA every role.
"""

import json
from pathlib import Path

import pytest
from web3 import Web3

from .abi import ANCHORING, ANCHORING_DEPLOYER
from .utils import fund, new_account, send_calls

pytestmark = pytest.mark.tempo  # tempo 0x76 create/tx, gas in PATH_USD

ADMIN, REGISTRAR, STATUS_UPDATER = 1, 2, 3

# Record tuple fields (AnchoringRegistry.Record, see abi.py): views decode to plain tuples.
CHECKSUM, STATUS, INDEX, IS_LATEST = 2, 6, 8, 9

_ARTIFACT = json.loads((Path(__file__).parent / "artifacts" / "anchoring.json").read_text())
DEPLOYER_BYTECODE = _ARTIFACT["deployer_bytecode"]


class Anchoring:
    """A deployed registry bound to its node + address, wrapping read/write boilerplate."""

    def __init__(self, w3, chain_id, address):
        self.w3, self.chain_id, self.address = w3, chain_id, address

    async def read(self, fn):
        return await fn.call(self.w3, to=self.address)

    async def write(self, signer, fn, *, expect=1):
        receipt = await send_calls(
            self.w3,
            chain_id=self.chain_id,
            private_key=signer.key.hex(),
            calls=[{"to": self.address, "data": fn.data}],
            gas_limit=8_000_000,
        )
        assert receipt["status"] == expect
        return receipt

    async def add_registry(self, signer, name, description="", metadata=""):
        await self.write(signer, ANCHORING.fns.addRegistry(name, description, metadata))
        return await self.read(ANCHORING.fns.registryIdByName(name))

    async def add_record(self, signer, registry, uri, checksum, algo="SHA-256", metadata=""):
        await self.write(signer, ANCHORING.fns.addRecord(registry, uri, checksum, algo, metadata))


@pytest.fixture
async def anchoring(w3, chain_id, funded_account):
    """Deploy impl + proxy + init in one create tx; ``funded_account`` becomes owner + all roles."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": None, "data": DEPLOYER_BYTECODE}],
        gas_limit=25_000_000,  # deploys two contracts (impl + proxy); far above the 2M default
    )
    assert receipt["status"] == 1, "deployer create reverted"
    proxy = await ANCHORING_DEPLOYER.fns.registry().call(w3, to=receipt["contractAddress"])
    return Anchoring(w3, chain_id, Web3.to_checksum_address(proxy))


async def test_add_registry(anchoring, funded_account):
    assert await anchoring.add_registry(funded_account, "docs", "document registry", "{}") == 1
    assert await anchoring.read(ANCHORING.fns.registryCount()) == 1


async def test_add_record(anchoring, funded_account):
    await anchoring.add_registry(funded_account, "docs")
    await anchoring.add_record(funded_account, "docs", "ipfs://a", "0xabc")

    rec = await anchoring.read(ANCHORING.fns.getRecord(1, 1, 0))
    assert rec[CHECKSUM] == "0xabc"
    assert rec[STATUS] == "active"
    assert rec[IS_LATEST] is True
    assert await anchoring.read(ANCHORING.fns.versionCount(1, 1)) == 1


async def test_same_checksum_maintains_record_id_and_versions(anchoring, funded_account):
    await anchoring.add_registry(funded_account, "docs")
    await anchoring.add_record(funded_account, "docs", "ipfs://v1", "0xabc")
    await anchoring.add_record(funded_account, "docs", "ipfs://v2", "0xabc")

    assert await anchoring.read(ANCHORING.fns.versionCount(1, 1)) == 2
    assert (await anchoring.read(ANCHORING.fns.getRecord(1, 1, 0)))[IS_LATEST] is False
    latest = await anchoring.read(ANCHORING.fns.getRecord(1, 1, 1))
    assert latest[INDEX] == 1 and latest[IS_LATEST] is True
    # Same checksum kept the same recordId.
    assert await anchoring.read(ANCHORING.fns.recordIdForChecksum(1, "0xabc")) == 1


async def test_per_registry_isolation_and_cross_registry_refs(anchoring, funded_account):
    await anchoring.add_registry(funded_account, "regA")
    await anchoring.add_registry(funded_account, "regB")
    await anchoring.add_record(funded_account, "regA", "ipfs://a", "0xshared")
    await anchoring.add_record(funded_account, "regB", "ipfs://b", "0xshared")

    # Independent per-registry recordId sequences (both start at 1)...
    assert await anchoring.read(ANCHORING.fns.recordIdForChecksum(1, "0xshared")) == 1
    assert await anchoring.read(ANCHORING.fns.recordIdForChecksum(2, "0xshared")) == 1
    # ...but the checksum is referenced from both registries.
    assert await anchoring.read(ANCHORING.fns.checksumRefCount("0xshared")) == 2


async def test_query_by_checksum_respects_limit(anchoring, funded_account):
    for name in ("r1", "r2", "r3"):
        await anchoring.add_registry(funded_account, name)
        await anchoring.add_record(funded_account, name, f"ipfs://{name}", "0xshared")

    assert await anchoring.read(ANCHORING.fns.checksumRefCount("0xshared")) == 3
    assert len(await anchoring.read(ANCHORING.fns.queryByChecksum("0xshared", 2))) == 2
    assert len(await anchoring.read(ANCHORING.fns.queryByChecksum("0xshared", 10))) == 3


async def test_update_record_status(anchoring, funded_account):
    await anchoring.add_registry(funded_account, "docs")
    await anchoring.add_record(funded_account, "docs", "ipfs://a", "0xabc")

    await anchoring.write(funded_account, ANCHORING.fns.updateRecordStatus(1, 1, 0, "redacted"))
    assert (await anchoring.read(ANCHORING.fns.getRecord(1, 1, 0)))[STATUS] == "redacted"


async def test_grant_role_lets_new_registrar_add(anchoring, funded_account):
    alice = new_account()
    await fund(anchoring.w3, alice.address)  # so alice can pay gas
    await anchoring.write(funded_account, ANCHORING.fns.grantRole(alice.address, REGISTRAR))
    assert await anchoring.read(ANCHORING.fns.hasRole(alice.address, REGISTRAR)) is True
    assert await anchoring.add_registry(alice, "alice-docs") == 1


async def test_unauthorized_add_reverts(anchoring):
    stranger = new_account()
    await fund(anchoring.w3, stranger.address)
    await anchoring.write(stranger, ANCHORING.fns.addRegistry("nope", "", ""), expect=0)
