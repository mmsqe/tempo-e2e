from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from tempo.devnet.cluster import ClusterCLI
from tempo.devnet.supervisor import DOCKER_CONFIG_FILE


class DockerCluster:
    """Manage a tempo devnet running under ``docker compose``."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        # ClusterCLI is reused only for config + RPC-URL resolution (no supervisord).
        self._cli = ClusterCLI(self.data_dir)
        self.compose_file = self.data_dir / DOCKER_CONFIG_FILE
        # Unique project per data dir — else Compose derives it from the dir
        # basename ("data") and ``ps -a`` reports a prior run's exited containers.
        digest = hashlib.sha1(str(self.data_dir).encode()).hexdigest()[:8]
        self.project = f"tempo-devnet-{digest}"
        self._log_followers: list[tuple[subprocess.Popen, object]] = []

    def _cmd(self, *args: str) -> list[str]:
        return ["docker", "compose", "-p", self.project, "-f", str(self.compose_file), *args]

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(self._cmd(*args), capture_output=True, text=True, check=check)

    def up(self) -> None:
        self._run("up", "-d")

    def down(self) -> None:
        self._run("down", "-t", "5", check=False)

    def logs(self, tail: int = 80) -> str:
        return self._run("logs", "--no-color", "--tail", str(tail), check=False).stdout

    def start_log_followers(self) -> None:
        for val in self._cli.config.validators:
            node_dir = self.data_dir / val.dir_name
            if not node_dir.is_dir():
                continue
            handle = open(node_dir / "node.log", "w")
            proc = subprocess.Popen(
                self._cmd("logs", "-f", "--no-color", "--no-log-prefix", val.moniker),
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
            self._log_followers.append((proc, handle))

    def stop_log_followers(self) -> None:
        for proc, handle in self._log_followers:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            handle.close()
        self._log_followers = []

    def crashed(self) -> list[str]:
        """Services whose container has exited."""
        return [r.get("Service", "?") for r in self._ps() if r.get("State") == "exited"]

    def _ps(self) -> list[dict]:
        """``docker compose ps`` rows (handles both NDJSON and array output)."""
        out = self._run("ps", "-a", "--format", "json").stdout.strip()
        if not out:
            return []
        try:
            parsed = json.loads(out)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [json.loads(line) for line in out.splitlines() if line.strip()]

    def node_rpc_url(self, moniker: str) -> str:
        return self._cli.node_rpc_url(moniker)

    def start_node(self, moniker: str) -> None:
        self._run("start", moniker)

    def stop_node(self, moniker: str) -> None:
        self._run("stop", moniker)

    def start_all(self) -> None:
        self._run("start")

    def stop_all(self) -> None:
        self._run("stop")
