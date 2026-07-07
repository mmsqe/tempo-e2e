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

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-p", self.project, "-f", str(self.compose_file), *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    def up(self) -> None:
        self._run("up", "-d")

    def down(self) -> None:
        self._run("down", "-t", "5", check=False)

    def logs(self, tail: int = 80) -> str:
        # docker-run.sh streams each node's stdout+stderr to <node_dir>/node.log,
        # so read the tail of those files.
        chunks = []
        for val in self._cli.config.validators:
            log = self.data_dir / val.dir_name / "node.log"
            if not log.exists():
                continue
            tail_lines = log.read_text(errors="replace").splitlines()[-tail:]
            chunks.append(f"=== {val.moniker} ===\n" + "\n".join(tail_lines))
        if chunks:
            return "\n\n".join(chunks)
        # Fallback for a tempo-py without the node.log redirect: docker's own logs.
        return self._run("logs", "--no-color", "--tail", str(tail), check=False).stdout

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
