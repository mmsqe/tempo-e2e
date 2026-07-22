"""Launch and manage a local ``tempo node --dev`` (the ``tempo-dev-up`` recipe) for e2e tests."""

from __future__ import annotations

import os
import shutil
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
    """``$TEMPO_BIN``, else ``tempo`` on PATH, else a local build, else ``cargo run``.

    Prefers the installed binary (``which tempo`` → ``~/.cargo/bin/tempo``) so an
    installed toolchain is used by default; set ``$TEMPO_BIN`` (or ``--tempo-bin``)
    to point at a specific build instead.
    """
    env_bin = os.environ.get("TEMPO_BIN")
    if env_bin:
        return [env_bin]
    on_path = shutil.which("tempo")
    if on_path:
        return [on_path]
    base = tempo_dir()
    for candidate in (base / "target/release/tempo", base / "target/debug/tempo"):
        if candidate.exists():
            return [str(candidate)]
    return ["cargo", "run", "--bin", "tempo", "--manifest-path", str(base / "Cargo.toml"), "--"]


def resolve_tempo_bin() -> str:
    """The built tempo binary as a single path (the devnet run scripts exec one path)."""
    parts = resolve_binary()
    if len(parts) == 1:
        return parts[0]
    raise RuntimeError("the devnet needs a built tempo binary (set TEMPO_BIN or build ../tempo)")


def resolve_xtask_bin() -> str:
    """``$TEMPO_XTASK_BIN``, else ``tempo-xtask`` on PATH, else a built ``tempo-xtask``."""
    env_bin = os.environ.get("TEMPO_XTASK_BIN")
    if env_bin:
        return env_bin
    on_path = shutil.which("tempo-xtask")
    if on_path:
        return on_path
    base = tempo_dir()
    for candidate in (base / "target/release/tempo-xtask", base / "target/debug/tempo-xtask"):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("the devnet needs tempo-xtask (set TEMPO_XTASK_BIN, put it on PATH, or build ../tempo)")


def _poll_rpc(rpc_url: str, *, timeout: float, want_block: int, check_alive=None) -> int:
    """Poll ``rpc_url`` until it reports ``block >= want_block``; return its chain id.

    ``check_alive`` (if given) is called each tick and should raise if the node
    process has died, so a crash surfaces at once instead of after ``timeout``.
    """
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        if check_alive:
            check_alive()
        try:
            if w3.is_connected() and w3.eth.block_number >= want_block:
                return w3.eth.chain_id
        except Exception as e:  # noqa: BLE001 - RPC not up yet
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(f"tempo RPC {rpc_url} not ready after {timeout}s (last error: {last_err})")


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
        block_time: str | None = None,
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
        self.block_time = block_time or os.environ.get("TEMPO_BLOCK_TIME", "50ms")
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

        def check_alive():
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"tempo node exited early (code {self.proc.returncode}); see {self.log_path}")

        self.chain_id = _poll_rpc(self.rpc_url, timeout=timeout, want_block=want_block, check_alive=check_alive)
        return self

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


class ExternalNode:
    """A handle to an already-running node (``--tempo-rpc`` / ``$TEMPO_RPC``).

    Lets the suite attach to a node it does not own and leave it running:
    ``stop()`` is a no-op. ``ws_url`` is None unless supplied (only the
    eth_subscribe tests need it).
    """

    def __init__(self, rpc_url: str, ws_url: str | None = None):
        self.rpc_url = rpc_url
        self.ws_url = ws_url
        self.chain_id: int | None = None

    def wait_for_rpc(self, timeout: float = 60.0, want_block: int = 1) -> "ExternalNode":
        """Poll the RPC until it answers with ``block >= want_block``; cache the chain id."""
        self.chain_id = _poll_rpc(self.rpc_url, timeout=timeout, want_block=want_block)
        return self

    def stop(self) -> None:  # not ours — leave it running
        pass
