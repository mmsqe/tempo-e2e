"""Throwaway: the whole corpus committed as one root per registry.

Skipped unless ``ROOTED`` names the root, ``merkle``, ``sha256`` or ``mmr``; `corpus`
says where the export and the migrator come from.
"""

import json
import os

import pytest

from . import corpus

pytestmark = pytest.mark.tempo
if not os.environ.get("ROOTED"):
    pytest.skip("ROOTED is unset", allow_module_level=True)
ROOT = os.environ["ROOTED"]
SENDERS = int(os.environ.get("ROOTED_SENDERS", "1"))


async def test_rooted(w3, chain_id, tempo, factory, tmp_path):
    keys = await corpus.senders(w3, SENDERS)
    out = tmp_path / f"rooted-{ROOT}.jsonl"
    # Threshold zero: every registry goes in as a root, however few rows it holds.
    planned = corpus.plan(corpus.EXPORT, out, "--threshold=0", f"--root={ROOT}")
    assert planned.returncode == 0, planned.stderr[-400:]
    steps = corpus.steps_in(out)

    gas, secs = corpus.send(out, rpc=tempo.rpc_url, chain_id=chain_id, factory=factory.address, keys=keys, timeout=7200)
    result = {
        "path": f"root only, {ROOT}",
        "steps": steps,
        "gas": gas,
        "secs": round(secs, 1),
        "factory": factory.address,
    }
    print("RESULT " + json.dumps(result), flush=True)
