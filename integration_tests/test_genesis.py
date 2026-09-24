"""What ``tempo-xtask generate-genesis`` writes into the alloc for this chain.

The launch genesis is generated rather than patched by hand, so the contract a chain starts on is
the runtime the binary embeds. Nothing here needs a node: the generated JSON is the subject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD

from .abi import ANCHORING_ADDRESS
from .anchoring import RUNTIME_CODE
from .network import DEV_GENESIS_ACCOUNTS, generate_dev_genesis

pytestmark = [pytest.mark.tempo]


def _alloc(genesis: Path) -> dict[str, dict]:
    return {address.lower(): account for address, account in json.loads(genesis.read_text())["alloc"].items()}


def _strings(storage: dict[str, str]) -> list[bytes]:
    """The short strings a TIP-20 stores in a slot: its name, symbol and currency."""
    return [bytes.fromhex(v[2:66])[: bytes.fromhex(v[2:66])[31] // 2] for v in storage.values()]


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
    assert _strings(storage).count(b"nUSD") == 2, "name and symbol"


def test_the_deployment_gas_token_is_what_the_genesis_pays_fees_in(tmp_path):
    """A temporary token for fees before any stablecoin is bridged: every genesis account pays in
    it, and the coinbase, the only fee recipient xtask sets, takes it."""
    alloc = _alloc(generate_dev_genesis(tmp_path, gas_token_admin="0x" + "11" * 20))
    [token] = [a for a in alloc if a.startswith("0x20c0") and b"DONOTUSE" in _strings(alloc[a].get("storage", {}))]
    fee_tokens = [v[-40:] for v in alloc[FEE_MANAGER_ADDRESS.lower()]["storage"].values()]
    assert fee_tokens.count(token[2:]) == DEV_GENESIS_ACCOUNTS + 1, "every account, and the coinbase"
