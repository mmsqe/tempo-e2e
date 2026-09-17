"""What ``tempo-xtask generate-genesis`` writes into the alloc for this chain.

The launch genesis is generated rather than patched by hand, so the contracts a chain starts on
are the runtimes the binary embeds. Nothing here needs a node: the generated JSON is the subject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .abi import ANCHORING_ADDRESS, MODULE_ADMIN_ADDRESS
from .anchoring import MULTISIG_RUNTIME, RUNTIME_CODE
from .network import generate_dev_genesis

pytestmark = [pytest.mark.tempo]

# The member keys behind the old chain's `params.Admin`
OWNERS = [
    "0x1becd7f3beed7907e5a94980b074b51f8d2f4bed",
    "0x4de8c982bcc02663554425b324cb4d5e2b87de93",
    "0xbf13df9e8fd64aee9c2ea17efe7a142514eceb40",
]


def _alloc(genesis: Path) -> dict[str, dict]:
    return {address.lower(): account for address, account in json.loads(genesis.read_text())["alloc"].items()}


def _code(account: dict) -> bytes:
    return bytes.fromhex(account["code"].removeprefix("0x"))


def test_the_generator_places_the_contracts_this_checkout_built(tmp_path):
    """The runtimes the binary embeds have to be the submodule's build, or a chain would launch
    on bytes nobody here has seen. `cargo xtask anchoring-runtime --check` says the same, on the
    other side of the fence."""
    alloc = _alloc(generate_dev_genesis(tmp_path, module_admin_owners=OWNERS))

    anchoring = alloc[ANCHORING_ADDRESS.lower()]
    assert _code(anchoring) == RUNTIME_CODE, "the binary embeds a different anchoring build"
    assert anchoring["nonce"] == "0x1", "placed code carries the nonce a deployment would leave"

    admin = alloc[MODULE_ADMIN_ADDRESS.lower()]
    assert _code(admin) == MULTISIG_RUNTIME, "the binary embeds a different module admin build"
    # Frozen at genesis and shown nowhere on chain, so this is the only place they are checked.
    assert [admin["storage"][f"0x{slot:064x}"][-40:] for slot in range(len(OWNERS))] == [
        owner.removeprefix("0x") for owner in OWNERS
    ]


def test_a_genesis_without_owners_carries_neither_contract(tmp_path):
    """Upstream's own networks generate the same way and must not pick these up."""
    alloc = _alloc(generate_dev_genesis(tmp_path))
    assert ANCHORING_ADDRESS.lower() not in alloc
    assert MODULE_ADMIN_ADDRESS.lower() not in alloc
