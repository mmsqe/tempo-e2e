"""Throwaway: the whole corpus replayed row by row, chunk after chunk.

Skipped unless ``REPLAY_DIR`` is set. It holds the anchoring binary at
``skip-wt/target/release/nvnmchain-anchoring`` and one directory per chunk under
``chunks/``, each with a ``registries.json`` and a ``manifest.json``.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import fund, new_account

pytestmark = pytest.mark.tempo
if not os.environ.get("REPLAY_DIR"):
    pytest.skip("REPLAY_DIR is unset", allow_module_level=True)
S = Path(os.environ["REPLAY_DIR"])
BIN = S / "skip-wt/target/release/nvnmchain-anchoring"
CHUNKS = sorted((S / "chunks").iterdir())
PROGRESS = S / "replay-progress.jsonl"
SENDERS = 8


async def test_full_replay(w3, chain_id, tempo, factory):
    accounts = [new_account() for _ in range(SENDERS)]
    for a in accounts:
        await fund(w3, a.address)
    # A bid is held at its cap while the transaction is out: say what each sender holds, so
    # a run that is going to die broke says so in its first seconds.
    held = [await ERC20.fns.balanceOf(a.address).call(w3, to=PATH_USD) for a in accounts]
    print(f"{len(accounts)} senders funded, smallest holds {min(held):,}", flush=True)
    env = {**os.environ, "PRIVATE_KEYS": ",".join(a.key.hex() for a in accounts)}
    PROGRESS.write_text("")  # the node is built fresh per run, so the file describes this run only
    began, done_steps, done_gas, failed = time.monotonic(), 0, 0, []

    for i, chunk in enumerate(CHUNKS, 1):
        plan = S / "chunk-plan.jsonl"
        t0 = time.monotonic()
        with plan.open("w") as fh:  # straight to the file: a chunk's plan runs to gigabytes
            planned = subprocess.run(  # noqa: S603
                [
                    str(BIN),
                    "migrate",
                    f"--registries={chunk}/registries.json",
                    f"--manifest={chunk}/manifest.json",
                    "--export=/tmp/from-chain",
                    "--threshold=99999999",
                    "--skip-status=Active",
                ],
                stdout=fh,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3600,
                env={**os.environ, "CHAIN_ID": "1", "TIDX_URL": "http://127.0.0.1:1"},
            )
        assert planned.returncode == 0, planned.stderr[-400:]
        with plan.open("rb") as fh:
            steps = sum(1 for _ in fh)
        line = {"chunk": i, "of": len(CHUNKS), "steps": steps, "plan_secs": round(time.monotonic() - t0, 1)}

        t0 = time.monotonic()
        argv = [sys.executable, "-m", "integration_tests.send_plan", f"--plan={plan}", f"--rpc={tempo.rpc_url}"]
        argv += [f"--chain-id={chain_id}", f"--factory={factory.address}"]
        sent = subprocess.run(argv, capture_output=True, text=True, timeout=14400, env=env)  # noqa: S603
        secs = time.monotonic() - t0
        plan.unlink()
        if sent.returncode == 0:
            gas = int(sent.stdout.split()[-2].replace(",", ""))  # "sent N steps from K sender(s), G gas"
            done_steps, done_gas = done_steps + steps, done_gas + gas
            line |= {"gas": gas, "send_secs": round(secs, 1), "steps_per_sec": round(steps / secs, 1)}
        else:
            # A chunk failing costs that chunk, not the hours before it: note it and carry on.
            failed.append(i)
            line["failed"] = sent.stderr.strip()[-200:]
        line |= {"done_steps": done_steps, "done_gas": done_gas, "elapsed": round(time.monotonic() - began, 1)}
        with PROGRESS.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        print(json.dumps(line), flush=True)

    total = time.monotonic() - began
    summary = {
        "steps": done_steps,
        "gas": done_gas,
        "seconds": round(total, 1),
        "steps_per_sec": round(done_steps / total, 1),
    }
    print(json.dumps({**summary, "failed_chunks": failed}, indent=2))
    assert not failed, f"{len(failed)} chunk(s) did not land: {failed}"
