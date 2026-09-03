"""Driving the Registry contracts: a factory, the registries it deploys, and their roles.

Shared because two suites need it from different sides -- the contracts over JSON-RPC, and
the projections over the service that reads their log. Neither is a good home for the other,
so the deployment recipe lives here and the ``factory`` fixture in ``conftest``.
"""

import json
import re
from pathlib import Path

from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3

from .abi import REGISTRY as REG
from .abi import REGISTRY_DEPLOYER, REGISTRY_FACTORY
from .utils import call_revert, funded, send_call, send_calls

#: The roles a registry knows, as the right-padded ``bytes32`` the contract compares. Pinned
#: against ``ROLE_ADMIN``/``ROLE_EDITOR`` by the registry suite, like every other constant here.
ADMIN = b"admin".ljust(32, b"\x00")
EDITOR = b"editor".ljust(32, b"\x00")
#: What ``_resolveRole`` hashes an empty checksum to, and so the scope of every registry-level
#: role. The contract's own spelling; a wrong constant drops creators silently.
REGISTRY_SCOPE = keccak(b"")

#: One build of the contracts: the one-shot deployer's initcode and the ``RecordCategory`` enum
#: it was built with, so the two cannot come from different commits. See ``make contract-artifacts``.
ARTIFACT = json.loads((Path(__file__).parent / "artifacts" / "registry.json").read_text())

#: ``RecordCategory`` by snake_case name. The ABI carries it as a bare uint8, so the names are
#: derived from the vendored enum rather than mirrored here: a reorder in the contract moves
#: these values on the next regeneration instead of silently mislabelling.
RECORD_CATEGORY = {
    re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower(): value
    for value, name in enumerate(ARTIFACT["record_categories"])
}


def add_record_call(checksum, *, uri="ipfs://a", algo="sha256", metadata="{}", category=0, data_pointer=""):
    """An ``addRecord`` call, so its six positional arguments are spelled out in one place."""
    return REG.fns.addRecord(uri, checksum, algo, metadata, category, data_pointer)


class Deployed:
    """One deployed registry bound to its node + address, wrapping call boilerplate."""

    def __init__(self, w3, chain_id, address, deployment, creator):
        self.w3, self.chain_id, self.address = w3, chain_id, address
        #: the receipt its RegistryDeployed log lives in
        self.deployment = deployment
        #: whoever deployed it, and so its first admin
        self.creator = creator

    async def read(self, fn):
        return await fn.call(self.w3, to=self.address)

    async def write(self, signer, fn):
        return await send_call(self.w3, self.chain_id, signer, self.address, fn.data)

    async def expect_revert(self, sender, fn, selector):
        err = await call_revert(self.w3, self.address, fn.data, sender=sender.address)
        assert selector in err, err

    async def grant(self, signer, account, role, checksum=""):
        return await self.write(signer, REG.fns.grantRole(checksum, account.address, role))

    async def revoke(self, signer, account, role, checksum=""):
        return await self.write(signer, REG.fns.revokeRole(checksum, account.address, role))

    async def has_role(self, account, role, checksum="") -> bool:
        return await self.read(REG.fns.hasRole(checksum, account.address, role))

    async def add_record(self, signer, checksum, **fields):
        """Anchors a version of ``checksum``; returns the receipt the anchor landed in."""
        return await self.write(signer, add_record_call(checksum, **fields))

    async def set_status(self, signer, checksum, index, status):
        return await self.write(signer, REG.fns.updateRecordStatus(checksum, index, status))

    async def versions(self, checksum) -> int:
        """How many versions ``checksum`` has here, zero if it has none."""
        return await self.read(REG.fns.versionCount(keccak(text=checksum)))


def deployed_addresses(receipt, factory: str) -> list[str]:
    """Every registry a transaction deployed, in call order, out of the log that announces
    them: there is no on-chain set to read, and one transaction may deploy several. This is
    how any consumer that is not itself a contract learns an address, a migration included.
    The topic comes off the ABI declaration; the event test spells it out independently.
    """
    topic0 = HexBytes(REGISTRY_FACTORY.events.RegistryDeployed.topic)
    return [
        Web3.to_checksum_address(bytes(HexBytes(log["topics"][1]))[-20:])
        for log in receipt["logs"]
        if log["address"].lower() == factory.lower() and HexBytes(log["topics"][0]) == topic0
    ]


def deployed_address(receipt, factory: str) -> str:
    """The one registry a `deployRegistry` created."""
    (address,) = deployed_addresses(receipt, factory)
    return address


class Factory:
    """The deployed factory, and the recipe for getting registries out of it."""

    def __init__(self, w3, chain_id, address, owner):
        self.w3, self.chain_id, self.address, self.owner = w3, chain_id, address, owner

    async def deploy(self, signer, name="docs", description="", metadata=""):
        """Deploys a registry and returns it bound, carrying its deployment receipt."""
        receipt = await send_call(
            self.w3,
            self.chain_id,
            signer,
            self.address,
            REGISTRY_FACTORY.fns.deployRegistry(name, description, metadata).data,
        )
        address = deployed_address(receipt, self.address)
        return Deployed(self.w3, self.chain_id, address, receipt, signer)


async def deploy_factory(w3, chain_id) -> Factory:
    """Runs the shipped one-shot deployer; the deploying EOA becomes owner."""
    owner = await funded(w3)
    initcode = bytes.fromhex(ARTIFACT["deployer_bytecode"][2:])
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=owner.key.hex(),
        calls=[{"to": None, "data": initcode}],
        # A create inside a create, and the factory carries a whole registry's creation code:
        # far above the 2M default, and still under the 30M per-tx cap.
        gas_limit=25_000_000,
    )
    assert receipt["status"] == 1, "deployer create reverted"
    deployer = receipt["contractAddress"]
    address = await REGISTRY_DEPLOYER.fns.factory().call(w3, to=deployer)
    return Factory(w3, chain_id, Web3.to_checksum_address(address), owner)
