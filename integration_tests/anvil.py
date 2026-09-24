"""An anvil standing in for Ethereum beside the tempo node: the bridge's second chain."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .network import _poll_rpc, _resolve_bin, free_port, terminate_process_group

# anvil's default mnemonic. Attestors never transact, so only their addresses matter.
DEPLOYER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ALICE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
RELAYER_KEY = "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6"
SECOND_RELAYER_KEY = "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a"
ATTESTOR_KEYS = (
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
)


def resolve_anvil_bin() -> str:
    return _resolve_bin("anvil", "ANVIL_BIN")


class AnvilNode:
    """A locally launched ``anvil``, ready to ``start()``."""

    def __init__(
        self,
        *,
        log_path: Path,
        chain_id: int = 1,
        http_port: int | None = None,
        block_time: str = "1",
        fork_url: str | None = None,
    ):
        self.log_path = Path(log_path)
        self.chain_id = chain_id
        self.fork_url = fork_url
        self.http_port = http_port or free_port()
        # On a timer, not on demand: the attestor waits on block numbers, not on our transactions.
        self.block_time = block_time
        self.binary = resolve_anvil_bin()
        self.proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        cmd = [self.binary, "--port", str(self.http_port), "--block-time", self.block_time, "--silent"]
        # A fork keeps its chain's id, which the attestors sign into their domains. Its reads go
        # upstream, so retry a rate-limited endpoint rather than fail the run.
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
