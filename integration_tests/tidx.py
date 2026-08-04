"""tempoxyz/tidx as a differential oracle for the indexer RPCs (see ``test_indexer.py``).

tidx is a standalone indexer: it polls a node's JSON-RPC for blocks/txs/logs into
PostgreSQL and exposes SQL over HTTP at ``GET /query``. It does not serve
``eth_getTransactions`` or the ``token_`` namespace -- what it gives the suite is a
second, independent ingest path over the same chain, queryable in O(1) instead of a
block walk.

Only PostgreSQL is enabled, which covers ``blocks``/``txs``/``logs``/``receipts``.
tidx's token projections are ClickHouse-only, so the ``token_`` endpoints have no
oracle here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIDX_IMAGE = os.environ.get("TIDX_IMAGE", "ghcr.io/tempoxyz/tidx:latest")
# Published as linux/amd64 only, so an arm64 host emulates it. Set TIDX_PLATFORM="" to
# drop the pin, which is what a natively-built $TIDX_IMAGE needs.
TIDX_PLATFORM = os.environ.get("TIDX_PLATFORM", "linux/amd64")
HOST_ALIAS = os.environ.get("TIDX_HOST_ALIAS", "host.docker.internal")

COMPOSE = """\
name: {project}

services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: tidx
      POSTGRES_USER: tidx
      POSTGRES_PASSWORD: tidx
    command: ["postgres", "-c", "synchronous_commit=off", "-c", "fsync=off"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tidx -d tidx"]
      interval: 1s
      timeout: 5s
      retries: 60

  tidx:
    image: {image}
{platform}
    entrypoint: ["sh", "-ec"]
    command:
      - |
        cat >/tmp/config.toml <<'EOF'
        [http]
        enabled = true
        port = 8080
        bind = "0.0.0.0"

        [prometheus]
        enabled = false

        [[chains]]
        name = "e2e"
        chain_id = {chain_id}
        rpc_url = "{rpc_url}"
        backfill = true
        batch_size = 50
        concurrency = 1

        # Not the root-level `pg_url` that tidx's own compose.yml still uses -- the
        # published image removed that field and refuses to start without this section.
        [chains.postgres]
        url = "postgres://tidx:tidx@postgres:5432/tidx"

        [chains.clickhouse]
        enabled = false
        EOF
        exec tidx up --config /tmp/config.toml
    environment:
      RUST_LOG: tidx=info
    extra_hosts:
      - "host.docker.internal:host-gateway"
    ports:
      - "{api_port}:8080"
    depends_on:
      postgres:
        condition: service_healthy
    # No restart policy on purpose: a crash should surface as "exited" for _dead()
    # rather than being masked by a restart loop that only shows up as a timeout.
"""


class TidxUnavailable(RuntimeError):
    """tidx cannot run here (no docker, no image, unreachable node) -- tests skip."""


class Tidx:
    """A tidx + PostgreSQL stack indexing ``rpc_url``, controlled over docker compose."""

    def __init__(self, base: Path, *, chain_id: int, rpc_url: str, api_port: int):
        self.base = Path(base)
        self.chain_id = chain_id
        self.rpc_url = rpc_url
        self.api_port = api_port
        self.project = f"tidx-e2e-{api_port}"
        self.compose_file = self.base / "tidx-compose.yml"

    # ---------------------------------------------------------------- lifecycle

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-f", str(self.compose_file), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def up(self) -> Tidx:
        self.base.mkdir(parents=True, exist_ok=True)
        self.compose_file.write_text(
            COMPOSE.format(
                project=self.project,
                image=TIDX_IMAGE,
                platform=f"    platform: {TIDX_PLATFORM}" if TIDX_PLATFORM else "",
                chain_id=self.chain_id,
                rpc_url=self.rpc_url,
                api_port=self.api_port,
            )
        )
        try:
            self._compose("up", "-d")
        except subprocess.CalledProcessError as e:
            raise TidxUnavailable(f"docker compose up failed:\n{e.stderr.strip()}") from e
        return self

    def down(self) -> None:
        self._compose("down", "-v", "--remove-orphans", check=False)

    def logs(self, tail: int = 40) -> str:
        return self._compose("logs", "--tail", str(tail), check=False).stdout

    def _dead(self) -> bool:
        """True once the tidx container has exited, so a crash fails fast instead of
        burning the whole wait timeout."""
        return "tidx" in self._compose("ps", "-a", "--status", "exited", "--services", check=False).stdout.split()

    # ------------------------------------------------------------------ queries

    def _get(self, path: str, params: dict, timeout: float = 30.0) -> dict:
        url = f"http://127.0.0.1:{self.api_port}{path}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed localhost URL
            return json.loads(resp.read())

    def sql(self, query: str, *, limit: int = 1000) -> list[dict]:
        """Run a SELECT and return rows as dicts.

        tidx answers ``{"columns": [...], "rows": [[...]], "row_count": n}``; zipping
        them here keeps the oracle assertions readable at the call site.
        """
        result = self._get("/query", {"sql": query, "chainId": self.chain_id, "limit": limit})
        return [dict(zip(result["columns"], row)) for row in result["rows"]]

    def synced_block(self) -> int:
        """Highest block written to PostgreSQL, or -1 before the first one.

        ``blocks`` numbers its column ``num``; only ``txs``/``logs``/``receipts`` call
        it ``block_num``.
        """
        rows = self.sql("SELECT max(num) AS head FROM blocks")
        head = rows[0]["head"] if rows else None
        return -1 if head is None else int(head)

    def wait_for_block(self, number: int, timeout: float = 180.0) -> int:
        """Block until tidx has indexed through ``number``.

        tidx polls, so it trails the chain and every oracle read has to pass through
        here or it races the sync loop. ``number=0`` doubles as the readiness check:
        the API may still be booting, so transport errors are retried, and the default
        timeout is generous because a first poll under arm64 emulation is slow.
        """
        deadline = time.time() + timeout
        head, last = -1, None
        while time.time() < deadline:
            if self._dead():
                raise TidxUnavailable(f"tidx container exited:\n{self.logs()}")
            try:
                head = self.synced_block()
                if head >= number:
                    return head
            except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
                last = e  # API still booting, or schema not migrated yet
            time.sleep(0.5)
        raise TidxUnavailable(
            f"tidx stalled at block {head}, waiting for {number} ({timeout}s, last error: {last})\n{self.logs()}"
        )


def _platform_args() -> list[str]:
    return ["--platform", TIDX_PLATFORM] if TIDX_PLATFORM else []


def preflight() -> None:
    """Raise ``TidxUnavailable`` unless docker and the tidx image are both usable."""
    if shutil.which("docker") is None:
        raise TidxUnavailable("the docker CLI is not available")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise TidxUnavailable("the docker daemon is not running")
    if subprocess.run(["docker", "image", "inspect", TIDX_IMAGE], capture_output=True).returncode != 0:
        raise TidxUnavailable(
            f"tidx image {TIDX_IMAGE!r} not found locally. Pull it first:\n"
            f"  {' '.join(['docker', 'pull', *_platform_args(), TIDX_IMAGE])}\n"
            f"(or point $TIDX_IMAGE at an image you have)"
        )
    # Pulling an amd64-only image succeeds on any host; *running* it needs emulation that
    # may be off. Probe now, so that shows up as a skip here rather than as a container
    # that never indexes a block and times out in wait_for_block().
    probe = subprocess.run(
        ["docker", "run", "--rm", *_platform_args(), TIDX_IMAGE, "--version"], capture_output=True, text=True
    )
    if probe.returncode != 0:
        raise TidxUnavailable(
            f"{TIDX_IMAGE} will not run here (exit {probe.returncode}): {(probe.stderr or probe.stdout).strip()}\n"
            f"tidx publishes {TIDX_PLATFORM or 'that image'} only, so an arm64 host needs emulation: turn on "
            "Docker Desktop → Settings → General → 'Use Rosetta for x86_64/amd64 emulation', or install the "
            "QEMU handlers with `docker run --privileged --rm tonistiigi/binfmt --install amd64`."
        )


def rpc_url_for_container(rpc_url: str) -> str:
    """``rpc_url`` as the tidx container sees it.

    A node on the host's loopback is only reachable through docker's host alias;
    an external node (``--tempo-rpc``) is already routable from the container.
    """
    parts = urllib.parse.urlparse(rpc_url)
    if parts.hostname not in ("127.0.0.1", "localhost", "0.0.0.0"):
        return rpc_url
    return parts._replace(netloc=f"{HOST_ALIAS}:{parts.port or 80}").geturl()
