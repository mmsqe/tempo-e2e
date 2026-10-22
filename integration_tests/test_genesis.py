"""What ``tempo-xtask generate-genesis`` writes into the alloc for this chain; no node needed.

The launch genesis is generated, not hand-patched, so a chain starts on the runtime the binary embeds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eth_utils import keccak
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS

from .abi import ANCHORING_ADDRESS
from .anchoring import IMPLEMENTATION, IMPLEMENTATION_SLOT, LAUNCH_RUNTIME_HASH, OWNER_SLOT, PROXY_CODE
from .network import DEV_GENESIS_ACCOUNTS, generate_dev_genesis

pytestmark = [pytest.mark.tempo]


def _alloc(genesis: Path) -> dict[str, dict]:
    return {address.lower(): account for address, account in json.loads(genesis.read_text())["alloc"].items()}


def _slots(account: dict) -> dict[int, int]:
    return {int(slot, 16): int(value, 16) for slot, value in account["storage"].items()}


def _strings(storage: dict[str, str]) -> list[bytes]:
    """The short strings a TIP-20 stores in a slot: its name, symbol and currency."""
    return [bytes.fromhex(v[2:66])[: bytes.fromhex(v[2:66])[31] // 2] for v in storage.values()]


class TestAnchoring:
    """The proxy on a chain starting at T12, the ownerless launch build on one scheduling it later,
    and nothing without ``--anchoring``."""

    OWNER = "0x" + "a0" * 20
    T12_LATER = {"t12_time": 1}

    def test_a_chain_starting_at_t12_launches_on_the_proxy(self, tmp_path):
        """The proxy points at where the implementation is deployed after launch."""
        alloc = _alloc(generate_dev_genesis(tmp_path, anchoring=True))
        account = alloc[ANCHORING_ADDRESS.lower()]
        # The validator registry keeps its owner in the low 20 bytes of its first slot.
        validator_admin = _slots(alloc[VALIDATOR_CONFIG_V2_ADDRESS.lower()])[0] & (2**160 - 1)

        assert bytes.fromhex(account["code"].removeprefix("0x")) == PROXY_CODE
        assert account["nonce"] == "0x1", "placed code carries the nonce a deployment would leave"
        assert _slots(account) == {OWNER_SLOT: validator_admin, IMPLEMENTATION_SLOT: int(IMPLEMENTATION, 16)}, (
            "every other slot is the dump's"
        )
        assert IMPLEMENTATION.lower() not in alloc

    def test_the_generator_names_the_owner(self, tmp_path):
        alloc = _alloc(generate_dev_genesis(tmp_path, anchoring=True, anchoring_owner=self.OWNER))
        assert _slots(alloc[ANCHORING_ADDRESS.lower()])[OWNER_SLOT] == int(self.OWNER, 16)

    def test_a_chain_scheduling_t12_launches_on_the_ownerless_build(self, tmp_path):
        alloc = _alloc(generate_dev_genesis(tmp_path, anchoring=True, fork_times=self.T12_LATER))
        account = alloc[ANCHORING_ADDRESS.lower()]
        assert keccak(bytes.fromhex(account["code"].removeprefix("0x"))) == LAUNCH_RUNTIME_HASH
        assert "storage" not in account

    def test_a_chain_scheduling_t12_cannot_name_the_owner(self, tmp_path):
        with pytest.raises(RuntimeError, match="--anchoring-owner needs T12 at genesis"):
            generate_dev_genesis(tmp_path, anchoring=True, anchoring_owner=self.OWNER, fork_times=self.T12_LATER)

    def test_a_genesis_without_the_flag_carries_no_contract(self, tmp_path):
        """Upstream's own networks generate the same way and must not pick it up."""
        assert ANCHORING_ADDRESS.lower() not in _alloc(generate_dev_genesis(tmp_path))


def test_the_reserved_stablecoin_is_named_nusd(tmp_path):
    storage = _alloc(generate_dev_genesis(tmp_path))[PATH_USD.lower()]["storage"]
    assert _strings(storage).count(b"nUSD") == 2, "name and symbol"


def test_the_deployment_gas_token_is_what_the_genesis_pays_fees_in(tmp_path):
    """The genesis accounts and the validator hold and pay in the temporary fee token, and the coinbase
    and validator take it; the validator is named by address, as a real network's are, so it is funded too."""
    alloc = _alloc(generate_dev_genesis(tmp_path, gas_token_admin="0x" + "11" * 20, validator_address="0x" + "22" * 20))
    [token] = [a for a in alloc if a.startswith("0x20c0") and b"DONOTUSE" in _strings(alloc[a].get("storage", {}))]
    fee_tokens = [v[-40:] for v in alloc[FEE_MANAGER_ADDRESS.lower()]["storage"].values()]
    assert fee_tokens.count(token[2:]) == DEV_GENESIS_ACCOUNTS + 3, (
        "every account and the validator; the coinbase and the validator"
    )
    minted = [v for v in alloc[token]["storage"].values() if int(v, 16) == 2**64 - 1]
    assert len(minted) == DEV_GENESIS_ACCOUNTS + 2, "every account, the issuer and the validator"
