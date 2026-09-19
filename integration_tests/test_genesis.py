"""What ``tempo-xtask generate-genesis`` writes into the alloc for this chain.

The launch genesis is generated rather than patched by hand, so the contracts a chain starts on
are the runtimes the binary embeds. Nothing here needs a node: the generated JSON is the subject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_utils import keccak

from .abi import ANCHORING_ADDRESS, MODULE_ADMIN_ADDRESS
from .anchoring import RUNTIME_CODE
from .network import generate_dev_genesis

pytestmark = [pytest.mark.tempo]

# The member keys behind the old chain's `params.Admin`
OWNERS = [
    "0x1becd7f3beed7907e5a94980b074b51f8d2f4bed",
    "0x4de8c982bcc02663554425b324cb4d5e2b87de93",
    "0xbf13df9e8fd64aee9c2ea17efe7a142514eceb40",
]

# Safe v1.4.1 as mainnet carries it: where its proxies point, the code at each address, and the
# storage it reads.
SAFE_SINGLETON_ADDRESS = "0x41675c099f32341bf84bfc5382af534df5c7461a"
SAFE_PROXY_CODE_HASH = "0xd7d408ebcd99b2b70be43e20253d6d92a8ea8fab29bd3be7f55b10032331fb4c"
SAFE_SINGLETON_CODE_HASH = "0x1fe2df852ba3299d6534ef416eefa406e56ced995bca886ab7a553e6d0c5e1c4"
SENTINEL = "0x" + "00" * 19 + "01"
SINGLETON_SLOT, OWNERS_SLOT, OWNER_COUNT_SLOT, THRESHOLD_SLOT = 0, 2, 3, 4
THRESHOLD = 2


def _alloc(genesis: Path) -> dict[str, dict]:
    return {address.lower(): account for address, account in json.loads(genesis.read_text())["alloc"].items()}


def _code(account: dict) -> bytes:
    return bytes.fromhex(account["code"].removeprefix("0x"))


def _slot(account: dict, slot: int) -> str:
    return account.get("storage", {}).get(f"0x{slot:064x}", "0x" + "0" * 64)


def _entry(account: dict, key: str) -> str:
    """`mapping(address => address) owners` at ``key``, as an address."""
    padded = bytes.fromhex(key.removeprefix("0x")).rjust(32, b"\0")
    return "0x" + _slot(account, int.from_bytes(keccak(padded + OWNERS_SLOT.to_bytes(32, "big")), "big"))[-40:]


@pytest.fixture(scope="module")
def launch_alloc(tmp_path_factory):
    """The alloc a launch genesis carries, generated once."""
    return _alloc(generate_dev_genesis(tmp_path_factory.mktemp("genesis"), module_admin_owners=OWNERS))


def test_the_generator_places_the_contracts_this_checkout_built(launch_alloc):
    """The runtime the binary embeds has to be the submodule's build, or a chain would launch on
    bytes nobody here has seen. `cargo xtask anchoring-runtime --check` says the same, on the other
    side of the fence."""
    anchoring = launch_alloc[ANCHORING_ADDRESS.lower()]
    assert _code(anchoring) == RUNTIME_CODE, "the binary embeds a different anchoring build"
    assert anchoring["nonce"] == "0x1", "placed code carries the nonce a deployment would leave"


def test_the_module_admin_is_a_safe_already_set_up(launch_alloc):
    """Genesis has no transaction to run Safe's ``setup()`` with, so it writes what setup writes.
    Frozen there and shown nowhere on chain, so this is the only place the owners are checked."""
    admin = launch_alloc[MODULE_ADMIN_ADDRESS.lower()]
    # The bytes, not the shape: the singleton's own runtime here would read back as a Safe.
    assert "0x" + keccak(_code(admin)).hex() == SAFE_PROXY_CODE_HASH, "the admin is not Safe's proxy"
    singleton = "0x" + _slot(admin, SINGLETON_SLOT)[-40:]
    assert singleton == SAFE_SINGLETON_ADDRESS, "the proxy delegates somewhere unexpected"
    assert "0x" + keccak(_code(launch_alloc[singleton])).hex() == SAFE_SINGLETON_CODE_HASH
    # An unset threshold lets the first caller `setup()` the singleton and be it.
    assert int(_slot(launch_alloc[singleton], THRESHOLD_SLOT), 16) == 1, "the singleton is claimable"

    # getOwners walks the list from the sentinel back to it, so a break anywhere hides an owner.
    walked, at = [], SENTINEL
    for _ in OWNERS:
        at = _entry(admin, at)
        walked.append(at)
    assert walked == OWNERS
    assert _entry(admin, walked[-1]) == SENTINEL, "the owner list does not close"
    assert int(_slot(admin, OWNER_COUNT_SLOT), 16) == len(OWNERS), "ownerCount disagrees with the list"
    assert int(_slot(admin, THRESHOLD_SLOT), 16) == THRESHOLD


def test_a_genesis_without_owners_carries_neither_contract(tmp_path):
    """Upstream's own networks generate the same way and must not pick these up."""
    alloc = _alloc(generate_dev_genesis(tmp_path))
    assert ANCHORING_ADDRESS.lower() not in alloc
    assert MODULE_ADMIN_ADDRESS.lower() not in alloc
    assert SAFE_SINGLETON_ADDRESS not in alloc
