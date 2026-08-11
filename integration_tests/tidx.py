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

from hexbytes import HexBytes

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


class QueryRefused(RuntimeError):
    """tidx rejected a query. Distinct from ``TidxUnavailable``: the stack is up and
    answering, the SQL is what it would not take, so this fails rather than skips."""


def bytea(value) -> str:
    """A byte string as PostgreSQL's SQL literal. ClickHouse takes ``'0x…'``, and a
    predicate carried between them matches nothing -- an empty result, not an error.

    Uncast, though PostgreSQL would take ``::bytea``: the cast is redundant, and tidx's
    pushdown extractor reads a bare literal but not a cast expression.
    """
    return "'\\x" + HexBytes(value).hex() + "'"


def named_signature(event) -> str:
    """A contract event in the form tidx's ``signature=`` takes: argument names become
    result columns, ``indexed`` says which come from the topics.

    Built from the ABI rather than typed out -- a signature tidx cannot match builds its
    table off a different topic0, which returns no rows rather than an error.
    """
    return "{}({})".format(
        event.name,
        ", ".join(
            f"{a['type']}{' indexed' if a.get('indexed') else ''} {a.get('name', '')}"
            for a in event.abi.get("inputs", [])
        ),
    )


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
        # doseq, so a list value becomes one repeated parameter rather than its repr --
        # ``?signature=A&signature=B``, which is how tidx takes more than one event.
        url = f"http://127.0.0.1:{self.api_port}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed localhost URL
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # A refusal puts the reason in the body of a 4xx. Unread, the caller sees only
            # "HTTP Error 422", naming neither the table nor the clause.
            raise QueryRefused(f"{e.code} {e.reason}: {e.read().decode(errors='replace')}\n  {url}") from e

    def sql(self, query: str, *, signature: str | list[str] | None = None, limit: int = 1000) -> list[dict]:
        """Run a SELECT and return rows as dicts.

        tidx answers ``{"columns": [...], "rows": [[...]], "row_count": n}``; zipping them
        here keeps the assertions readable at the call site. ``signature`` generates a
        decoded event table named after the event; without one only the base tables exist.
        Several may be passed, each becoming its own table.

        A refusal comes back 200 with ``ok: false``, so an unchecked read turns SQL tidx
        would not take into an empty result set -- what a correct query over an empty chain
        also returns.
        """
        params = {"sql": query, "chainId": self.chain_id, "limit": limit}
        if signature is not None:
            params["signature"] = [signature] if isinstance(signature, str) else list(signature)
        result = self._get("/query", params)
        if not result.get("ok"):
            raise QueryRefused(f"{result.get('error', result)}\n  sql: {query}")
        return [dict(zip(result["columns"], row)) for row in result["rows"]]

    def coverage(self) -> dict:
        """The chain's ``/status`` entry: ``tip_num``, ``synced_num``, ``backfill_num``, gaps.

        Not ``SELECT ... FROM sync_state`` -- ``/query`` allowlists blocks/txs/logs/receipts
        and 422s the rest, that table by name. For which field may bound an index-derived
        answer, see ``indexed_through``.
        """
        status = self._get("/status", {})
        for chain in status.get("chains", []):
            if int(chain["chain_id"]) == self.chain_id:
                return chain
        raise TidxUnavailable(f"chain {self.chain_id} is not in /status: {status}")

    def indexed_through(self) -> int:
        """The highest block an index-derived answer may be pinned to.

        ``tip_num``, not ``synced_num``: realtime ingest advances the former and leaves the
        latter to gap-fill, so on an index following the chain ``synced_num`` sits below
        rows already there and a query bounded by it reads almost nothing while reporting
        clean.
        """
        return int(self.coverage()["tip_num"])

    def bounded(self, receipt) -> int:
        """``indexed_through`` after waiting for ``receipt``'s block: what a test does
        between writing and querying. Asserted to cover the block, because a bound below
        it drops the rows under test and returns what a correct query over an unwritten
        chain also returns."""
        wrote = receipt["blockNumber"]
        self.wait_for_block(wrote)
        at = self.indexed_through()
        assert at >= wrote, f"index reached {at}, but the test wrote at {wrote}"
        return at

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
            except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError, QueryRefused) as e:
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
