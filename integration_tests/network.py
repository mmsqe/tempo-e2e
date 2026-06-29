"""Launch and manage a local ``tempo node --dev`` (the ``tempo-dev-up`` recipe) for e2e tests."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from web3 import Web3

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TEMPO_DIR = REPO_ROOT.parent / "tempo"

# Prefunded dev key the faucet sends from, and the TIP-20 stablecoin it funds.
FAUCET_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
FAUCET_TOKEN = "0x20c0000000000000000000000000000000000000"
FAUCET_AMOUNT = 1_000_000_000_000_000

DEFAULT_HTTP_PORT = 8545


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tempo_dir() -> Path:
    return Path(os.environ.get("TEMPO_DIR", DEFAULT_TEMPO_DIR)).resolve()


def resolve_binary() -> list[str]:
    """``$TEMPO_BIN``, else a release/debug build, else a ``cargo run`` fallback."""
    env_bin = os.environ.get("TEMPO_BIN")
    if env_bin:
        return [env_bin]
    base = tempo_dir()
    for candidate in (base / "target/release/tempo", base / "target/debug/tempo"):
        if candidate.exists():
            return [str(candidate)]
    return ["cargo", "run", "--bin", "tempo", "--manifest-path", str(base / "Cargo.toml"), "--"]


def default_genesis() -> Path:
    """The permissive dev genesis the node's own tests use (override with ``$TEMPO_GENESIS``)."""
    env_genesis = os.environ.get("TEMPO_GENESIS")
    if env_genesis:
        return Path(env_genesis).resolve()
    base = tempo_dir()
    test_genesis = base / "crates" / "node" / "tests" / "assets" / "test-genesis.json"
    if test_genesis.exists():
        return test_genesis
    return base / "scripts" / "genesis" / "staccato.json"


class TempoNode:
    """A locally launched ``tempo node --dev`` instance."""

    def __init__(
        self,
        *,
        datadir: Path,
        log_path: Path,
        http_port: int = DEFAULT_HTTP_PORT,
        ws_port: int | None = None,
        p2p_port: int | None = None,
        genesis: Path | None = None,
        binary: list[str] | None = None,
        block_time: str = "1sec",
    ):
        self.datadir = Path(datadir)
        self.log_path = Path(log_path)
        self.http_port = http_port
        self.ws_port = ws_port or free_port()
        # Randomize the P2P + auth-RPC ports so multiple nodes can run at once (xdist, node-ops).
        self.p2p_port = p2p_port or free_port()
        self.auth_port = free_port()
        self.genesis = Path(genesis) if genesis else default_genesis()
        self.binary = binary or resolve_binary()
        self.block_time = block_time
        self.proc: subprocess.Popen | None = None
        self.chain_id: int | None = None

    def command(self) -> list[str]:
        return [
            *self.binary,
            "node",
            "--chain",
            str(self.genesis),
            "--datadir",
            str(self.datadir),
            "--dev",
            "--dev.block-time",
            self.block_time,
            "--http",
            "--http.addr",
            "127.0.0.1",
            "--http.port",
            str(self.http_port),
            "--http.api",
            "all",
            "--ws",
            "--ws.addr",
            "127.0.0.1",
            "--ws.port",
            str(self.ws_port),
            "--ws.api",
            "all",
            "--port",
            str(self.p2p_port),
            "--authrpc.port",
            str(self.auth_port),
            "--disable-discovery",
            "--engine.disable-precompile-cache",
            "--builder.gaslimit",
            "3000000000",
            # max-tasks must be 1 when the engine shares the sparse trie with the builder.
            "--builder.max-tasks",
            "1",
            "--builder.deadline",
            "3",
            "--faucet.enabled",
            "--faucet.private-key",
            FAUCET_PRIVATE_KEY,
            "--faucet.amount",
            str(FAUCET_AMOUNT),
            "--faucet.address",
            FAUCET_TOKEN,
            "--faucet.node-address",
            f"http://127.0.0.1:{self.http_port}",
        ]

    def start(self) -> "TempoNode":
        self.datadir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            self.command(),
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(self.genesis.parent),
            start_new_session=True,
        )
        return self

    def wait_for_rpc(self, timeout: float = 180.0, want_block: int = 1) -> "TempoNode":
        """Poll the HTTP RPC until it answers with ``block >= want_block``; cache the chain id."""
        w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        deadline = time.time() + timeout
        last_err: Exception | None = None
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"tempo node exited early (code {self.proc.returncode}); see {self.log_path}")
            try:
                if w3.is_connected() and w3.eth.block_number >= want_block:
                    self.chain_id = w3.eth.chain_id
                    return self
            except Exception as e:  # noqa: BLE001 - RPC not up yet
                last_err = e
            time.sleep(0.5)
        raise TimeoutError(f"tempo RPC not ready after {timeout}s (last error: {last_err}); see {self.log_path}")

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}"
