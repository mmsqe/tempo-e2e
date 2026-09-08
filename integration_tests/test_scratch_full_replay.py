"""Measured: the whole corpus replayed row by row, chunk after chunk.

Skipped unless ``REPLAY_DIR`` is set: a writable directory with room for two chunk plans at
once, tens of gigabytes. The chunks are cut from the export on the way in; `corpus` says
where the export and the migrator come from.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from . import corpus

pytestmark = pytest.mark.tempo
if not os.environ.get("REPLAY_DIR"):
    pytest.skip("REPLAY_DIR is unset", allow_module_level=True)
S = Path(os.environ["REPLAY_DIR"])
PROGRESS = S / "replay-progress.jsonl"
SENDERS = 8
# Statuses the planner leaves out, as the corpus holds one value and it carries nothing.
# Empty replays them too, which doubles the corpus and is the other half worth measuring.
SKIP_STATUS = os.environ.get("REPLAY_SKIP_STATUS", "Active")
# Every registry row by row, however large: the opposite leg to `test_scratch_rooted`.
FLAGS = ("--threshold=99999999", *([f"--skip-status={SKIP_STATUS}"] if SKIP_STATUS else []))


async def test_full_replay(w3, chain_id, tempo, factory):
    chunks = corpus.chunks(S / "chunks")
    keys = await corpus.senders(w3, SENDERS)
    # A bid is held at its cap while the transaction is out: say what each sender holds, so
    # a run that is going to die broke says so in its first seconds.
    held = [await ERC20.fns.balanceOf(k.address).call(w3, to=PATH_USD) for k in keys]
    print(f"{len(keys)} senders funded, smallest holds {min(held):,}", flush=True)
    PROGRESS.write_text("")  # the node is built fresh per run, so the file describes this run only
    began, done_steps, done_gas, done_bytes, failed = time.monotonic(), 0, 0, 0, []

    def plan_chunk(chunk, out):
        t0 = time.monotonic()
        return corpus.plan(chunk, out, *FLAGS), round(time.monotonic() - t0, 1)

    # The next chunk is planned while this one is sent: planning is CPU the sender would
    # otherwise wait out. Two plan files alternate, so the one being sent is never overwritten
    # -- two chunks' plans on disk at once, a couple of gigabytes -- and a plan that fails is
    # noticed one chunk late, when its turn comes.
    plans = [S / "chunk-plan-a.jsonl", S / "chunk-plan-b.jsonl"]
    with ThreadPoolExecutor(max_workers=1) as planner:
        pending = planner.submit(plan_chunk, chunks[0], plans[0])
        for i in range(1, len(chunks) + 1):
            out = plans[(i - 1) % 2]
            planned, plan_secs = pending.result()
            if i < len(chunks):
                pending = planner.submit(plan_chunk, chunks[i], plans[i % 2])
            assert planned.returncode == 0, planned.stderr[-400:]
            steps, plan_bytes = corpus.steps_in(out), out.stat().st_size
            done_bytes += plan_bytes
            line = {"chunk": i, "of": len(chunks), "steps": steps, "plan_bytes": plan_bytes, "plan_secs": plan_secs}

            try:
                gas, secs = corpus.send(
                    out, rpc=tempo.rpc_url, chain_id=chain_id, factory=factory.address, keys=keys, timeout=14400
                )
            except RuntimeError as stopped:
                # A chunk failing costs that chunk, not the hours before it: note it and carry on.
                failed.append(i)
                line["failed"] = str(stopped)[-200:]
            else:
                done_steps, done_gas = done_steps + steps, done_gas + gas
                line |= {"gas": gas, "send_secs": round(secs, 1), "steps_per_sec": round(steps / secs, 1)}
            out.unlink()
            line |= {"done_steps": done_steps, "done_gas": done_gas, "done_bytes": done_bytes}
            line["elapsed"] = round(time.monotonic() - began, 1)
            with PROGRESS.open("a") as fh:
                fh.write(json.dumps(line) + "\n")
            print(json.dumps(line), flush=True)

    total = time.monotonic() - began
    # One ``RESULT`` line, the same shape `test_scratch_rooted` reports, so whatever drives
    # the run reads all four paths the same way.
    result = {
        "path": f"1:1 replay, {'--skip-status' if SKIP_STATUS else 'with status'}",
        "steps": done_steps,
        "gas": done_gas,
        "plan_bytes": done_bytes,
        "seconds": round(total, 1),
        "steps_per_sec": round(done_steps / total, 1),
        "failed_chunks": failed,
        # Every registry on this chain hangs off it, and `reconcile` takes it as
        # `FACTORY_ADDRESS`. A fresh one per run, so the summary is the only record.
        "factory": factory.address,
    }
    print("RESULT " + json.dumps(result), flush=True)
    assert not failed, f"{len(failed)} chunk(s) did not land: {failed}"
