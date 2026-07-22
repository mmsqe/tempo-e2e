"""Standalone dev-node control: ``python -m integration_tests.devnode up|down``.

``up`` launches a tempo dev node in the foreground (Ctrl-C to stop); ``down``
terminates any running ``tempo node`` process. ``up --datadir PATH`` resumes an
existing single-node datadir (e.g. a kept pytest node) so an explorer can index
its state.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .network import DEFAULT_HTTP_PORT, TempoNode


def _up(datadir: Path, log_path: Path, http_port: int, genesis: Path | None) -> int:
    node = TempoNode(datadir=datadir, log_path=log_path, http_port=http_port, genesis=genesis)
    node.start()
    print(f"tempo dev node starting (pid {node.proc.pid}); RPC at {node.rpc_url}")
    print(f"datadir: {node.datadir}")
    print(f"logs: {node.log_path}")
    try:
        node.wait_for_rpc()
        print(f"RPC ready at {node.rpc_url} (chain id {node.chain_id})")
        print(f"WS at {node.ws_url}")
        # Attach the e2e suite or an explorer to this node:
        print(f"  pytest --tempo-rpc {node.rpc_url} --tempo-ws {node.ws_url}")
        print(f"  TEMPO_RPC={node.rpc_url} CHAIN_ID={node.chain_id}  # for tempo-explorer-py")
        node.proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
    return 0


def _down() -> int:
    result = subprocess.run(["pkill", "-f", "tempo node"])
    print("killed tempo node processes" if result.returncode == 0 else "no tempo node processes found")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="devnode", description="Standalone tempo dev-node control.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up", help="launch a dev node in the foreground (Ctrl-C to stop)")
    up.add_argument(
        "--datadir",
        type=Path,
        default=Path("node-data") / "data",
        help="node datadir; point at a kept single-node datadir to resume it (default: ./node-data/data)",
    )
    up.add_argument(
        "--http-port", type=int, default=DEFAULT_HTTP_PORT, help=f"HTTP RPC port (default {DEFAULT_HTTP_PORT})"
    )
    up.add_argument(
        "--genesis",
        type=Path,
        default=None,
        help="chain genesis (default: the dev test genesis; must match the datadir when resuming)",
    )
    sub.add_parser("down", help="terminate any running tempo node")

    args = parser.parse_args(argv)
    if args.cmd == "down":
        return _down()
    return _up(args.datadir, args.datadir.parent / "tempo.log", args.http_port, args.genesis)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
