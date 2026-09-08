"""Driving the migrator and the sender over a corpus, for the throwaway measurements.

The export, its ``registries.json`` and ``manifest.json`` beside it, is under
``NVNM_EXPORT_DIR`` -- the same one the service tests read. The migrator is
``MIGRATE_BIN``, or whatever ``cargo install`` put on the path.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from .utils import fund, new_account

BIN = os.environ.get("MIGRATE_BIN", os.path.expanduser("~/.cargo/bin/nvnmchain-anchoring"))
EXPORT = Path(os.environ.get("NVNM_EXPORT_DIR", "/tmp/from-chain"))
# The migrator reads a chain to reconcile against. These leg it away from one: the plan is
# built from the export alone, so nothing here should reach a node but the sender.
OFFLINE = {"CHAIN_ID": "1", "TIDX_URL": "http://127.0.0.1:1"}


def plan(source: Path, out: Path, *flags: str, timeout: float = 3600) -> subprocess.CompletedProcess:
    """One directory's plan, straight to ``out``: a chunk's plan runs to gigabytes."""
    argv = [
        str(BIN),
        "migrate",
        f"--registries={source}/registries.json",
        f"--manifest={source}/manifest.json",
        f"--export={EXPORT}",
        *flags,
    ]
    with out.open("w") as fh:
        return subprocess.run(  # noqa: S603
            argv, stdout=fh, stderr=subprocess.PIPE, text=True, timeout=timeout, env={**os.environ, **OFFLINE}
        )


async def senders(w3, count: int) -> list:
    """``count`` funded accounts, for ``PRIVATE_KEYS``."""
    accounts = [new_account() for _ in range(count)]
    for account in accounts:
        await fund(w3, account.address)
    return accounts


def send(out: Path, *, rpc: str, chain_id: int, factory: str, keys: list, timeout: float) -> tuple[int, float]:
    """A plan sent from ``keys``, returning the gas it spent and how long it took."""
    argv = [sys.executable, "-m", "integration_tests.send_plan", f"--plan={out}", f"--rpc={rpc}"]
    argv += [f"--chain-id={chain_id}", f"--factory={factory}"]
    env = {**os.environ, "PRIVATE_KEYS": ",".join(k.key.hex() for k in keys)}
    began = time.monotonic()
    sent = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)  # noqa: S603
    secs = time.monotonic() - began
    if sent.returncode != 0:
        raise RuntimeError(sent.stderr.strip()[-600:])
    # "sent N steps from K sender(s), G gas"
    return int(sent.stdout.split()[-2].replace(",", "")), secs


def steps_in(out: Path) -> int:
    with out.open("rb") as fh:
        return sum(1 for _ in fh)
