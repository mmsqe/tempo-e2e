"""Standalone dev-node control: ``python -m integration_tests.devnode up|down``.

``up`` launches a tempo dev node in the foreground (Ctrl-C to stop); ``down``
terminates any running ``tempo node`` process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .network import DEFAULT_HTTP_PORT, TempoNode


def _up() -> int:
    base = Path("node-data")
    node = TempoNode(
        datadir=base / "data",
        log_path=base / "tempo.log",
        http_port=DEFAULT_HTTP_PORT,
    )
    node.start()
    print(f"tempo dev node starting (pid {node.proc.pid}); RPC at {node.rpc_url}")
    print(f"logs: {node.log_path}")
    try:
        node.wait_for_rpc()
        print(f"RPC ready at {node.rpc_url} (chain id {node.chain_id})")
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
    if len(argv) != 1 or argv[0] not in {"up", "down"}:
        print("usage: python -m integration_tests.devnode up|down", file=sys.stderr)
        return 2
    return _up() if argv[0] == "up" else _down()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
