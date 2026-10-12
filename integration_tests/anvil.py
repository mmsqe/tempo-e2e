"""An anvil standing in for Ethereum beside the tempo node: the bridge's second chain."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .network import _poll_rpc, _resolve_bin, free_port, terminate_process_group

# anvil's default mnemonic.
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ALICE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
RELAYER_KEY = "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6"
MILLION = 1_000_000 * 10**18  # NVNM has 18 decimals


class AnvilNode:
    """A locally launched ``anvil`` on chain id 1, or a fork of ``fork_url``, ready to ``start()``.
    Raises RuntimeError when there is no anvil to launch."""

    def __init__(self, *, log_path: Path, fork_url: str | None = None):
        self.log_path = Path(log_path)
        self.chain_id = 1
        self.fork_url = fork_url
        self.http_port = free_port()
        self.binary = _resolve_bin("anvil", "ANVIL_BIN")
        self.proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        cmd = [self.binary, "--port", str(self.http_port), "--silent"]
        # A fork keeps its chain's id, by which the live NVNM is found. Its reads go upstream, so
        # retry a rate-limited endpoint rather than fail the run.
        if self.fork_url:
            return [*cmd, "--fork-url", self.fork_url, "--retries", "10", "--timeout", "30000"]
        return [*cmd, "--chain-id", str(self.chain_id)]

    def start(self) -> "AnvilNode":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("w")
        self.proc = subprocess.Popen(
            self.command(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return self

    def wait_for_rpc(self, timeout: float = 60.0) -> "AnvilNode":
        def check_alive():
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"anvil exited with {self.proc.returncode}; see {self.log_path}")

        # Ready as soon as it answers; a fork reports the forked chain's id.
        self.chain_id = _poll_rpc(self.rpc_url, timeout=timeout, want_block=0, check_alive=check_alive)
        return self

    def stop(self) -> None:
        terminate_process_group(self.proc)
        self.proc = None

    @property
    def rpc_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"
