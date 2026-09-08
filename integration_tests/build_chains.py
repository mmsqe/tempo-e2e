"""One chain per row of the cost table, each left on disk to browse afterwards.

    python -m integration_tests.build_chains --out ~/nvnm-chains [row ...]

The `tempo` fixture prunes the datadir it made, so the scratch tests alone leave nothing to
open. This owns the node and hands the test its RPC, which is what ``--tempo-rpc`` is for.

Steps, gas and the plan's size come back from the test; the transaction count does not,
since nothing here counts transactions. That comes from indexing the chain afterwards.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .network import TempoNode, free_port, generate_dev_genesis

# A single-validator chain stops for good at its first epoch boundary. The default is
# 302,400 blocks and the full replay with statuses takes 332,649 of them, so the run would
# wedge with a fifth of the corpus left.
EPOCH_LENGTH = 1_000_000_000
BLOCK_TIME = "10ms"

# `REPLAY_SKIP_STATUS` defaults to "Active" when unset, so "send every status" is the empty
# string rather than the absent variable.
ROWS = {
    "root-mmr": {"ROOTED": "mmr"},
    "root-sha256": {"ROOTED": "sha256"},
    "replay-skip": {"REPLAY_SKIP_STATUS": "Active"},
    "replay-status": {"REPLAY_SKIP_STATUS": ""},
}
SUITE = Path(__file__).resolve().parent.parent


def build(name: str, env: dict, out: Path, genesis: Path) -> dict:
    home = out / name
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)

    node = TempoNode(
        datadir=home / "node0",
        log_path=home / "tempo.log",
        http_port=free_port(),
        genesis=genesis,
        block_time=BLOCK_TIME,
    )
    began = time.monotonic()
    node.start().wait_for_rpc()
    print(f"[{name}] node up, datadir {node.datadir}", flush=True)
    test = "test_scratch_rooted.py" if "ROOTED" in env else "test_scratch_full_replay.py"
    try:
        run = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", f"integration_tests/{test}", "-s", "-q", "--tempo-rpc", node.rpc_url],
            cwd=SUITE,
            capture_output=True,
            text=True,
            timeout=6 * 3600,
            # Beside the chain rather than in it: the plans run to tens of gigabytes, and
            # `datadir_gb` is meant to be what the chain itself costs.
            env={**os.environ, "REPLAY_DIR": str(home / "scratch"), **env},
        )
    finally:
        node.stop()

    grew = sum(f.stat().st_size for f in node.datadir.rglob("*") if f.is_file())
    line = {
        "chain": name,
        "ok": run.returncode == 0,
        "secs": round(time.monotonic() - began, 1),
        "datadir_gb": round(grew / 1e9, 2),
    }
    for printed in run.stdout.splitlines():
        if printed.startswith("RESULT "):  # steps, gas and the plan's size, from either test
            line |= json.loads(printed[7:])
    if run.returncode != 0:
        line["tail"] = (run.stdout + run.stderr).strip()[-400:]
    return line


def serve(name: str, out: Path, genesis: Path) -> None:
    """Re-open a chain built earlier and hold it up, for an indexer to read it back.

    A `--dev` node mints blocks whether or not anything is sent, so this runs slow: the
    transaction total does not move, but every block is one more for the indexer to walk.
    """
    home = out / name
    node = TempoNode(
        datadir=home / "node0",
        log_path=home / "serve.log",
        http_port=free_port(),
        genesis=genesis,
        block_time="5s",
    )
    node.start().wait_for_rpc()
    print(f"NVNM_RPC=http://127.0.0.1:{node.http_port}   (ctrl-c to stop)", flush=True)
    try:
        while node.proc and node.proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("rows", nargs="*", choices=list(ROWS), default=list(ROWS), help="default: all four")
    ap.add_argument("--out", type=Path, required=True, help="where each chain's datadir is left")
    ap.add_argument("--serve", action="store_true", help="re-open the named chains instead of building them")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    genesis = generate_dev_genesis(args.out / "genesis", epoch_length=EPOCH_LENGTH)
    if args.serve:
        for name in args.rows:
            serve(name, args.out, genesis)
        return

    summary = args.out / "summary.jsonl"
    for name in args.rows:
        line = build(name, ROWS[name], args.out, genesis)
        print("CHAIN " + json.dumps(line), flush=True)
        with summary.open("a") as fh:
            fh.write(json.dumps(line) + "\n")


if __name__ == "__main__":
    main()
