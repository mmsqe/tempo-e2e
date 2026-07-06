"""A local multi-validator (Simplex BFT) network, so the ``consensus_*`` RPC is served.

Built on tempo-py's devnet package (``tempo.devnet``): a YAML config drives
``tempo-xtask generate-localnet`` and every node runs under supervisord, so
per-node stop/start goes through the supervisor XML-RPC instead of raw PIDs.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml
from tempo.devnet.cli import init as devnet_init
from tempo.devnet.cluster import ClusterCLI
from tempo.devnet.ports import http_rpc_port
from web3 import Web3

from .network import free_port, resolve_binary, tempo_dir

_PORTS_PER_NODE = 6  # devnet port scheme: consensus, exec-p2p, metrics, authrpc, http, ws
_HEALTHY = {"RUNNING", "STARTING"}


def _can_bind(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _alloc_base_ports(n: int, stride: int = 10) -> list[int]:
    """Reserve ``n`` free base-port blocks of the devnet port scheme."""
    for _ in range(200):
        base = free_port()
        bases = [base + i * stride for i in range(n)]
        needed = [p + k for p in bases for k in range(_PORTS_PER_NODE)]
        if max(needed) < 65500 and all(_can_bind(p) for p in needed):
            return bases
    raise RuntimeError("could not find a free consensus port region")


def _tempo_bin() -> str:
    """The built tempo binary; the devnet run scripts exec a single path."""
    parts = resolve_binary()
    if len(parts) == 1:
        return parts[0]
    raise RuntimeError("the devnet needs a built tempo binary (set TEMPO_BIN or build ../tempo)")


def _xtask_bin() -> str:
    """``$TEMPO_XTASK_BIN``, else a built ``tempo-xtask``."""
    env_bin = os.environ.get("TEMPO_XTASK_BIN")
    if env_bin:
        return env_bin
    base = tempo_dir()
    for candidate in (base / "target/release/tempo-xtask", base / "target/debug/tempo-xtask"):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("the devnet needs a built tempo-xtask (set TEMPO_XTASK_BIN or build ../tempo)")


class ConsensusNetwork:
    """A locally launched N-validator consensus network (tempo-devnet under supervisord)."""

    def __init__(self, *, base_dir: Path, validators: int = 4, accounts: int = 200, epoch_length: int = 100):
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "data"
        self.validators = validators
        self.accounts = accounts
        self.epoch_length = epoch_length
        # Random free ports (baked into genesis) so runs don't collide with each other or a dev node.
        self.base_ports = _alloc_base_ports(validators)
        self.cluster: ClusterCLI | None = None
        self._supervisord: subprocess.Popen | None = None

    def _moniker(self, i: int) -> str:
        return f"node{i}"

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{http_rpc_port(self.base_ports[0])}"

    # -- lifecycle -----------------------------------------------------------
    def generate(self) -> "ConsensusNetwork":
        """Write the devnet YAML, then genesis + per-validator keys + supervisord.ini."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "chain_id": 1337,
            "accounts": self.accounts,
            "epoch_length": self.epoch_length,
            "seed": 0,
            "tempo_bin": _tempo_bin(),
            "tempo_xtask_bin": _xtask_bin(),
            "validators": [
                {"host": "127.0.0.1", "port": port, "moniker": self._moniker(i)}
                for i, port in enumerate(self.base_ports)
            ],
        }
        config_path = self.base_dir / "devnet.yaml"
        config_path.write_text(yaml.dump(config, default_flow_style=False))
        try:
            devnet_init(data=str(self.data_dir), config=str(config_path), force=True)
        except SystemExit as e:  # devnet_init exits on failure; surface it to pytest
            raise RuntimeError(f"tempo-devnet init failed (exit {e.code}); see {self.base_dir}") from e
        return self

    def start(self) -> "ConsensusNetwork":
        """Launch all nodes via ``tempo-devnet start`` (supervisord, nodaemon)."""
        log = open(self.base_dir / "supervisord.out", "a")
        self._supervisord = subprocess.Popen(
            [sys.executable, "-m", "tempo.devnet.cli", "start", "--data", str(self.data_dir)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.cluster = ClusterCLI(self.data_dir)
        deadline = time.time() + 30
        while time.time() < deadline:  # wait for the control socket, then for every node to launch
            try:
                if all(p["statename"] in _HEALTHY for p in self.cluster.status()):
                    return self
            except Exception:  # noqa: BLE001 - supervisord still booting
                pass
            time.sleep(0.5)
        raise RuntimeError(f"supervisord did not start all nodes; see {self.base_dir}")

    def wait_for_finalization(self, timeout: float = 120.0) -> "ConsensusNetwork":
        """Wait until validator 0 finalizes a block via consensus (not just produces one)."""
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            crashed = [p["name"] for p in self.cluster.status() if p["statename"] in ("FATAL", "EXITED")]
            if crashed:
                raise RuntimeError(f"validators crashed: {crashed}; see {self.data_dir}")
            try:
                finalized = (w3.provider.make_request("consensus_getLatest", []).get("result") or {}).get("finalized")
                if finalized and finalized.get("view", 0) >= 1 and w3.eth.block_number >= 1:
                    return self
            except Exception as e:  # noqa: BLE001 - consensus warming up
                last_err = e
            time.sleep(1.0)
        raise TimeoutError(
            f"consensus did not finalize within {timeout}s (last error: {last_err}); see {self.base_dir}"
        )

    def stop(self) -> None:
        if self._supervisord is None:
            return
        try:
            self.cluster.supervisor.shutdown()  # stops all nodes, then supervisord exits
        except Exception:  # noqa: BLE001 - socket already gone
            self._supervisord.terminate()
        try:
            self._supervisord.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._supervisord.kill()
            self._supervisord.wait(timeout=10)
        self._supervisord = None
