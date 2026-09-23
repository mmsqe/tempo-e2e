"""What ``tempo-xtask generate-genesis`` writes into the alloc for this chain.

The launch genesis is generated rather than patched by hand, so the contract a chain starts on is
the runtime the binary embeds. Nothing here needs a node: the generated JSON is the subject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tempo.constants import PATH_USD

from .abi import ANCHORING_ADDRESS
from .anchoring import RUNTIME_CODE
from .network import generate_dev_genesis

pytestmark = [pytest.mark.tempo]


def _alloc(genesis: Path) -> dict[str, dict]:
    return {address.lower(): account for address, account in json.loads(genesis.read_text())["alloc"].items()}


def test_the_generator_places_the_contract_this_checkout_built(tmp_path):
    """The runtime the binary embeds has to be the submodule's build, or a chain would launch on
    bytes nobody here has seen. `cargo xtask anchoring-runtime --check` says the same, on the other
    side of the fence."""
    account = _alloc(generate_dev_genesis(tmp_path, anchoring=True))[ANCHORING_ADDRESS.lower()]

    assert bytes.fromhex(account["code"].removeprefix("0x")) == RUNTIME_CODE
    assert account["nonce"] == "0x1", "placed code carries the nonce a deployment would leave"
    assert "storage" not in account, "every slot is the dump's, not the alloc's"


def test_a_genesis_without_the_flag_carries_no_contract(tmp_path):
    """Upstream's own networks generate the same way and must not pick it up."""
    assert ANCHORING_ADDRESS.lower() not in _alloc(generate_dev_genesis(tmp_path))


def test_the_reserved_stablecoin_is_named_nusd(tmp_path):
    storage = _alloc(generate_dev_genesis(tmp_path))[PATH_USD.lower()]["storage"]
    strings = [bytes.fromhex(v[2:66])[: bytes.fromhex(v[2:66])[31] // 2] for v in storage.values()]
    assert strings.count(b"nUSD") == 2, "name and symbol"
