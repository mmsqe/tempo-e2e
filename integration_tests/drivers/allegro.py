"""Driver for allegro — a reth + simplex-consensus node.

tempo-py's devnet tooling (``backend: allegro``) builds the genesis and each
node's reth-style command line; the shared ``TempoNode`` machinery runs the
processes. allegro has none of tempo's native features, so tempo-specific
tests skip against it.
"""

from __future__ import annotations

import json
from pathlib import Path

from tempo.devnet.backends import generate_localnet, get_backend
from tempo.devnet.config import DevnetConfig
from tempo.devnet.ports import http_rpc_port, ws_rpc_port
from web3 import AsyncWeb3

from ..network import TempoNode, _resolve_bin
from ..utils import fund_via_transfer, new_account
from .base import CAP_EMBEDDED_VALIDATORS, CAP_INDEXER, CAP_INDEXER_RPC

# Anvil account 0 — prefunded with 10_000 ETH in allegro's xtask genesis.
FUNDER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# Faster consensus for tests than the binary's defaults.
CONSENSUS_TEST_FLAGS = ["--consensus.leader-timeout", "1000", "--consensus.cert-timeout", "2000"]


class AllegroNode(TempoNode):
    """A ``TempoNode`` whose command line was prebuilt by tempo-py's allegro backend."""

    def __init__(self, *, node_index: int, node_args: list[str], **kwargs):
        super().__init__(**kwargs)
        self.node_index = node_index
        self.node_args = node_args

    def command(self) -> list[str]:
        return self.node_args

    def validator_count(self) -> int:
        """Validators embedded in this node's genesis.json."""
        return len(json.loads(self.genesis.read_text()).get("validators", []))


class AllegroDriver:
    name = "allegro"

    def __init__(self):
        self._bin: str | None = None
        self._xtask: str | None = None

    def capabilities(self) -> set[str]:
        # allegro serves eth_getTransactions from its own ExEx-backed index, so unlike
        # tempo the handler is real -- but the token_ namespace needs TIP-20, which
        # allegro has no equivalent of, so those tests stay skipped.
        #
        # CAP_INDEXER too, which base.py otherwise leaves to `--indexer` on the grounds
        # that a live index is a deployment property rather than a binary one. That
        # holds for a tidx sidecar; it does not hold here, because the index is the
        # node. Left flag-gated the semantic tier would run only when someone
        # remembered the flag, which in CI means never. If the indexer ever becomes
        # opt-in at the node (a `--indexer.*` flag), this must move behind whatever
        # `make_cluster` passes to enable it, or the claim goes stale in the one
        # direction that fails tests rather than skipping them.
        return {CAP_EMBEDDED_VALIDATORS, CAP_INDEXER_RPC, CAP_INDEXER}

    def resolve_bin(self) -> str:
        if self._bin is None:
            self._bin = _resolve_bin("allegro", "ALLEGRO_BIN")
        return self._bin

    def resolve_xtask(self) -> str:
        if self._xtask is None:
            self._xtask = _resolve_bin("allegro-xtask", "ALLEGRO_XTASK_BIN")
        return self._xtask

    def make_cluster(self, base: Path, n: int, *, chain_id: int = 1337) -> list[AllegroNode]:
        """An ``n``-validator devnet sharing one embedded-validator genesis;
        returns unstarted nodes (caller starts and waits)."""
        devnet = Path(base) / "devnet"
        cfg = DevnetConfig.ephemeral(
            n,
            backend="allegro",
            chain_id=chain_id,
            node_bin=self.resolve_bin(),
            xtask_bin=self.resolve_xtask(),
        )
        genesis = generate_localnet(cfg, devnet)
        backend = get_backend(cfg)
        nodes = []
        for i, val in enumerate(cfg.validators):
            datadir = devnet / val.dir_name
            nodes.append(
                AllegroNode(
                    binary=cfg.tempo_bin,
                    node_index=i,
                    node_args=backend.node_args(
                        cfg,
                        val,
                        i,
                        val_dir=datadir,
                        genesis_path=str(genesis),
                        datadir=str(datadir),
                        trusted_peers=[],
                        extra_flags=CONSENSUS_TEST_FLAGS,
                    ),
                    datadir=datadir,
                    genesis=genesis,
                    log_path=devnet / f"{val.dir_name}.log",
                    http_port=http_rpc_port(val.base_port),
                    ws_port=ws_rpc_port(val.base_port),
                )
            )
        return nodes

    def dev_node(self, base: Path, *, log_name: str = "allegro.log", **kwargs) -> AllegroNode:
        """A solo validator (quorum of one) for the standard-eth tests."""
        (node,) = self.make_cluster(base, 1, chain_id=kwargs.get("chain_id", 1337))
        node.log_path = Path(base) / "devnet" / log_name
        return node

    async def fund(self, w3: AsyncWeb3, address: str, amount: int) -> str:
        """Plain EIP-1559 transfer from the prefunded anvil account."""
        return await fund_via_transfer(w3, FUNDER_KEY, address, amount)

    async def send_tx(self, w3: AsyncWeb3, chain_id: int, sender) -> dict:
        """A plain EIP-1559 value transfer -- allegro has no native AA tx type."""
        tx_hash = await fund_via_transfer(w3, sender.key.hex(), new_account().address, 1)
        return await w3.eth.get_transaction_receipt(tx_hash)
