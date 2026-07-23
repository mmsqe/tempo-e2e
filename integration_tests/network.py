"""Launch and manage a local ``tempo node --dev`` (the ``tempo-dev-up`` recipe) for e2e tests."""

from __future__ import annotations

import functools
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from web3 import Web3

# Accounts prefunded in the generated dev genesis. --dev ignores baked-in validator,
# which is only there to satisfy the generator.
DEV_GENESIS_ACCOUNTS = 20

# Prefunded dev key the faucet sends from, and the TIP-20 stablecoin it funds.
FAUCET_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
FAUCET_TOKEN = "0x20c0000000000000000000000000000000000000"
FAUCET_AMOUNT = 1_000_000_000_000_000

DEFAULT_HTTP_PORT = 8545


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_bin(name: str, env_var: str) -> str:
    """``$<env_var>``, else ``name`` on PATH, else raise."""
    path = os.environ.get(env_var) or shutil.which(name)
    if not path:
        raise RuntimeError(f"the devnet needs {name} (set ${env_var}, put it on PATH, or build ../tempo)")
    return path


def resolve_tempo_bin() -> str:
    return _resolve_bin("tempo", "TEMPO_BIN")


def resolve_xtask_bin() -> str:
    return _resolve_bin("tempo-xtask", "TEMPO_XTASK_BIN")


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


def generate_dev_genesis(output_dir: Path) -> Path:
    """The dev genesis: ``$TEMPO_GENESIS`` if set, else generated in ``output_dir`` via ``tempo-xtask``.

    An already-generated ``genesis.json`` is reused (deterministic ``--seed 0``), so a node
    can restart on the same dir and CI can supply a prebuilt genesis without needing xtask.
    """
    env_genesis = os.environ.get("TEMPO_GENESIS")
    if env_genesis:
        return Path(env_genesis).resolve()
    genesis = output_dir / "genesis.json"
    if genesis.exists():
        return genesis
    output_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            resolve_xtask_bin(),
            "generate-genesis",
            "--output",
            str(output_dir),
            "--accounts",
            str(DEV_GENESIS_ACCOUNTS),
            "--seed",
            "0",
            "--validators",
            "127.0.0.1:30303",
            "--no-dkg-in-genesis",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not genesis.exists():
        raise RuntimeError(f"tempo-xtask generate-genesis failed (exit {result.returncode}):\n{result.stderr}")
    return genesis


@functools.lru_cache(maxsize=1)
def default_genesis() -> Path:
    """A dev genesis for a node built without an explicit one, once per process (see generate_dev_genesis)."""
    return generate_dev_genesis(Path(tempfile.mkdtemp(prefix="tempo-genesis-")))


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
        binary: str | None = None,
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
        self.binary = binary or resolve_tempo_bin()
        self.block_time = block_time or os.environ.get("TEMPO_BLOCK_TIME", "50ms")
        self.proc: subprocess.Popen | None = None
        self.chain_id: int | None = None

    def command(self) -> list[str]:
        return [
            self.binary,
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


def dev_node(base: Path, *, log_name: str = "node.log", **kwargs) -> TempoNode:
    """A ``--dev`` node with a fresh ``genesis.json`` beside its datadir.

    Lays out ``base/devnet/{genesis.json, node0}`` — the self-contained shape the
    session fixture uses. ``http_port`` defaults to a free port; extra keyword args
    (``block_time``, ...) pass through to ``TempoNode``.
    """
    devnet = base / "devnet"
    genesis = generate_dev_genesis(devnet)
    kwargs.setdefault("http_port", free_port())
    return TempoNode(datadir=devnet / "node0", log_path=devnet / log_name, genesis=genesis, **kwargs)


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
