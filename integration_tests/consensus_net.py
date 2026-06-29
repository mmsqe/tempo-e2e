"""A local multi-validator (Simplex BFT) network, so the ``consensus_*`` RPC is served.

Built from ``tempo-xtask generate-localnet``; validators peer over 127.0.0.1 on
distinct ports (``--consensus.bypass-ip-check``, no sudo/loopback aliases needed).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from web3 import Web3

from .network import free_port, resolve_binary, tempo_dir

# Passphrase the generated (age-encrypted) signing keys are sealed with.
SIGNING_KEY_PASSPHRASE = "tempo-localnet-signing-key-secret"


def _can_bind(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _alloc_region(n: int, stride: int = 10):
    """Reserve a free port region: ``n`` consensus blocks of 5 (listen, +1 p2p, +2
    metrics, +3 authrpc, +4 discv5) plus ``n`` RPC ports. Returns ``(consensus, http)``."""
    for _ in range(200):
        base = free_port()
        consensus = [base + i * stride for i in range(n)]
        http = [base + n * stride + i for i in range(n)]
        needed = [p + k for p in consensus for k in range(5)] + http
        if max(needed) < 65500 and all(_can_bind(p) for p in needed):
            return consensus, http
    raise RuntimeError("could not find a free consensus port region")


def resolve_xtask() -> list[str]:
    """``$TEMPO_XTASK_BIN``, else a built ``tempo-xtask``, else ``cargo run -p tempo-xtask``."""
    env_bin = os.environ.get("TEMPO_XTASK_BIN")
    if env_bin:
        return [env_bin]
    base = tempo_dir()
    for candidate in (base / "target/release/tempo-xtask", base / "target/debug/tempo-xtask"):
        if candidate.exists():
            return [str(candidate)]
    return ["cargo", "run", "-p", "tempo-xtask", "--manifest-path", str(base / "Cargo.toml"), "--"]


class ConsensusNetwork:
    """A locally launched N-validator consensus network."""

    def __init__(self, *, base_dir: Path, validators: int = 4, accounts: int = 200, epoch_length: int = 100):
        self.base_dir = Path(base_dir)
        self.validators = validators
        self.accounts = accounts
        self.epoch_length = epoch_length
        self.binary = resolve_binary()
        self.genesis = self.base_dir / "genesis.json"
        self.secret_path = self.base_dir / "secret.txt"
        self.procs: list[subprocess.Popen | None] = []
        # Random free ports (baked into genesis) so runs don't collide with each other or a dev node.
        self.consensus_ports, self.http_ports = _alloc_region(validators)

    def _addr(self, i: int) -> str:
        return f"127.0.0.1:{self.consensus_ports[i]}"

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.http_ports[0]}"

    def _trusted_peers(self) -> str:
        return ",".join(
            f"enode://{(self.base_dir / self._addr(i) / 'enode.identity').read_text().strip()}"
            f"@127.0.0.1:{self.consensus_ports[i] + 1}"
            for i in range(self.validators)
        )

    def _node_args(self, i: int, peers: str) -> list[str]:
        port = self.consensus_ports[i]
        datadir = self.base_dir / self._addr(i)
        return [
            *self.binary,
            "node",
            "--chain",
            str(self.genesis),
            "--datadir",
            str(datadir),
            "--consensus.signing-key",
            str(datadir / "signing.key"),
            "--consensus.secret",
            str(self.secret_path),
            "--consensus.signing-share",
            str(datadir / "signing.share"),
            "--consensus.listen-address",
            f"127.0.0.1:{port}",
            "--consensus.metrics-address",
            f"127.0.0.1:{port + 2}",
            "--trusted-peers",
            peers,
            "--port",
            str(port + 1),
            "--discovery.port",
            str(port + 1),
            "--discovery.v5.port",
            str(port + 4),
            "--p2p-secret-key",
            str(datadir / "enode.key"),
            "--authrpc.port",
            str(port + 3),
            "--http",
            "--http.addr",
            "127.0.0.1",
            "--http.port",
            str(self.http_ports[i]),
            "--http.api",
            "all",
            "--consensus.use-local-defaults",
            "--consensus.bypass-ip-check",
        ]

    # -- lifecycle -----------------------------------------------------------
    def generate(self) -> "ConsensusNetwork":
        """Write the genesis (validators registered on-chain) and per-validator keys."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        validators = ",".join(self._addr(i) for i in range(self.validators))
        subprocess.run(
            [
                *resolve_xtask(),
                "generate-localnet",
                "-o",
                str(self.base_dir),
                "--accounts",
                str(self.accounts),
                "--epoch-length",
                str(self.epoch_length),
                "--seed",
                "0",
                "--validators",
                validators,
                "--force",
            ],
            check=True,
            capture_output=True,
            cwd=str(tempo_dir()),
        )
        self.secret_path.write_text(SIGNING_KEY_PASSPHRASE + "\n")
        return self

    def _spawn(self, i: int, peers: str) -> subprocess.Popen:
        log = open(self.base_dir / f"node{i}.log", "a")
        return subprocess.Popen(self._node_args(i, peers), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def start(self) -> "ConsensusNetwork":
        peers = self._trusted_peers()
        self.procs = [self._spawn(i, peers) for i in range(self.validators)]
        return self

    def _term(self, proc: subprocess.Popen | None) -> None:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def stop_one(self, i: int) -> None:
        self._term(self.procs[i])
        self.procs[i] = None

    def start_one(self, i: int) -> None:
        self.procs[i] = self._spawn(i, self._trusted_peers())

    def wait_for_finalization(self, timeout: float = 120.0) -> "ConsensusNetwork":
        """Wait until validator 0 finalizes a block via consensus (not just produces one)."""
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            for proc in self.procs:
                if proc is not None and proc.poll() is not None:
                    raise RuntimeError(f"a validator exited early (code {proc.returncode}); see {self.base_dir}")
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
        for proc in self.procs:
            self._term(proc)
        self.procs = []
