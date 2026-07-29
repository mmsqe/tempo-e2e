"""Driver for allegro — a reth + simplex-consensus node.

allegro speaks standard Ethereum JSON-RPC with none of tempo's native txs,
fee tokens, faucet, or precompiles, so tempo-specific tests skip against it.
Validators are embedded in ``genesis.json``, tying each node's ``--listen``
port to the ingress baked at genesis time — genesis generation and node launch
share one contiguous consensus port block. A single validator is a quorum of
one, so a solo node backs the standard-eth tests like tempo's ``--dev`` node.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

from eth_account import Account
from web3 import AsyncWeb3, Web3

from ..network import _poll_rpc, _resolve_bin, terminate_process_group
from .base import CAP_EMBEDDED_VALIDATORS

# Anvil account 0 — prefunded with 10_000 ETH in allegro's xtask genesis.
FUNDER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _reserve_ports(n_consecutive: int, n_extra: int) -> tuple[list[int], list[int]]:
    """Reserve ``n_consecutive`` contiguous ports plus ``n_extra`` arbitrary ones.

    All are bound simultaneously so they are mutually distinct, then released
    just before returning; start the processes promptly.
    """
    for _ in range(50):
        held: list[socket.socket] = []
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            base = probe.getsockname()[1]
            held.append(probe)
            consecutive = [base]
            for k in range(1, n_consecutive):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.bind(("127.0.0.1", base + k))
                except OSError:
                    s.close()
                    break
                held.append(s)
                consecutive.append(base + k)
            if len(consecutive) != n_consecutive:
                continue  # not a long enough contiguous run; retry
            extra = []
            for _ in range(n_extra):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("127.0.0.1", 0))
                held.append(s)
                extra.append(s.getsockname()[1])
            return consecutive, extra
        finally:
            for s in held:
                s.close()
    raise RuntimeError(f"could not reserve {n_consecutive} contiguous + {n_extra} ports")


class AllegroNode:
    """One ``allegro --execution reth`` process, addressable over JSON-RPC."""

    def __init__(
        self,
        *,
        binary: str,
        node_index: int,
        datadir: Path,
        genesis: Path,
        log_path: Path,
        consensus_port: int,
        peer_consensus_ports: list[int],
        http_port: int,
        auth_port: int,
        reth_p2p_port: int,
    ):
        self.binary = binary
        self.node_index = node_index
        self.datadir = Path(datadir)
        self.genesis = Path(genesis)
        self.log_path = Path(log_path)
        self.consensus_port = consensus_port
        self.peer_consensus_ports = peer_consensus_ports
        self.http_port = http_port
        self.auth_port = auth_port
        self.reth_p2p_port = reth_p2p_port
        self.proc: subprocess.Popen | None = None
        self.chain_id: int | None = None

    def command(self) -> list[str]:
        cmd = [
            self.binary,
            "--execution",
            "reth",
            "--node",
            str(self.node_index),
            "--listen",
            f"127.0.0.1:{self.consensus_port}",
            "--leader-timeout",
            "1000",
            "--cert-timeout",
            "2000",
            "--datadir",
            str(self.datadir),
            "--rpc-port",
            str(self.http_port),
            "--authrpc-port",
            str(self.auth_port),
            "--reth-p2p-port",
            str(self.reth_p2p_port),
            "--genesis",
            str(self.genesis),
        ]
        for port in self.peer_consensus_ports:
            cmd += ["--peer", f"127.0.0.1:{port}"]
        return cmd

    def start(self) -> "AllegroNode":
        self.datadir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w")
        env = {**os.environ, "RUST_LOG": os.environ.get("RUST_LOG", "allegro=info,commonware=warn")}
        self.proc = subprocess.Popen(
            self.command(), stdout=subprocess.DEVNULL, stderr=log, start_new_session=True, env=env
        )
        return self

    def wait_for_rpc(self, timeout: float = 60.0, want_block: int = 1) -> "AllegroNode":
        def check_alive():
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"allegro node {self.node_index} exited early (code {self.proc.returncode}); see {self.log_path}"
                )

        self.chain_id = _poll_rpc(self.rpc_url, timeout=timeout, want_block=want_block, check_alive=check_alive)
        return self

    def stop(self) -> None:
        terminate_process_group(self.proc)
        self.proc = None

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def ws_url(self) -> str | None:
        return None  # allegro's CLI does not expose a WS endpoint

    def validator_count(self) -> int:
        """Validators embedded in this node's genesis.json."""
        return len(json.loads(self.genesis.read_text()).get("validators", []))


class AllegroDriver:
    name = "allegro"

    def __init__(self):
        self._bin: str | None = None
        self._xtask: str | None = None

    def capabilities(self) -> set[str]:
        return {CAP_EMBEDDED_VALIDATORS}  # standard eth + genesis-embedded validator set

    def resolve_bin(self) -> str:
        if self._bin is None:
            self._bin = _resolve_bin("allegro", "ALLEGRO_BIN")
        return self._bin

    def resolve_xtask(self) -> str:
        if self._xtask is None:
            self._xtask = _resolve_bin("allegro-xtask", "ALLEGRO_XTASK_BIN")
        return self._xtask

    def generate_genesis(self, out_dir: Path, *, validators: int, chain_id: int, base_port: int) -> Path:
        """Run ``allegro-xtask genesis``; returns the embedded-validator genesis.json."""
        out_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                self.resolve_xtask(),
                "genesis",
                "--validators",
                str(validators),
                "--base-port",
                str(base_port),
                "--chain-id",
                str(chain_id),
                "--output",
                str(out_dir),
            ],
            capture_output=True,
            text=True,
        )
        genesis = out_dir / "genesis.json"
        if result.returncode != 0 or not genesis.exists():
            raise RuntimeError(f"allegro-xtask genesis failed (exit {result.returncode}):\n{result.stderr}")
        return genesis

    def make_cluster(self, base: Path, n: int, *, chain_id: int = 1337) -> list[AllegroNode]:
        """An ``n``-validator devnet sharing one embedded-validator genesis;
        returns unstarted nodes (caller starts and waits)."""
        base = Path(base)
        # n contiguous consensus ports (baked into genesis) + 3 reth ports per node,
        # all distinct so a peer's --listen never collides with our RPC port.
        consensus_ports, reth_ports = _reserve_ports(n, 3 * n)
        base_port = consensus_ports[0]
        genesis = self.generate_genesis(base / "devnet", validators=n, chain_id=chain_id, base_port=base_port)
        binary = self.resolve_bin()
        nodes = []
        for i in range(n):
            nodes.append(
                AllegroNode(
                    binary=binary,
                    node_index=i,
                    datadir=base / "devnet" / f"node-{i}",
                    genesis=genesis,
                    log_path=base / "devnet" / f"node-{i}.log",
                    consensus_port=consensus_ports[i],
                    peer_consensus_ports=[p for j, p in enumerate(consensus_ports) if j != i],
                    http_port=reth_ports[3 * i],
                    auth_port=reth_ports[3 * i + 1],
                    reth_p2p_port=reth_ports[3 * i + 2],
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
        funder = Account.from_key(FUNDER_KEY)
        base_fee = (await w3.eth.get_block("latest")).get("baseFeePerGas") or 0
        priority = Web3.to_wei(1, "gwei")
        nonce = await w3.eth.get_transaction_count(funder.address, "pending")
        tx = {
            "chainId": await w3.eth.chain_id,
            "nonce": nonce,
            "to": AsyncWeb3.to_checksum_address(address),
            "value": amount,
            "gas": 21_000,
            "maxPriorityFeePerGas": priority,
            "maxFeePerGas": base_fee * 2 + priority,
            "type": 2,
        }
        signed = funder.sign_transaction(tx)
        tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
        await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60.0)
        return tx_hash.hex()
