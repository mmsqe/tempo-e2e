"""Snapshots: `tempo snapshot-manifest` archives a stopped node, `tempo download` restores it.

A dev node keeps no finalizations, so its round trip skips consensus; a follower's carries them.
"""

import functools
import json
import re
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from web3 import Web3

from .network import FollowerNode, TempoNode, dev_node, free_port, node_log, resolve_tempo_bin
from .utils import poll_height

pytestmark = [pytest.mark.tempo, pytest.mark.slow]


def _tempo(*args: str) -> None:
    done = subprocess.run(
        [resolve_tempo_bin(), *args], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL
    )
    if done.returncode != 0:
        raise RuntimeError(f"tempo {args[0]} failed (exit {done.returncode}):\n{done.stderr[-2000:]}")


def _snapshot(datadir: Path, archives: Path, chain_id: int, *args: str) -> dict:
    """Archive a stopped node's datadir; return the manifest."""
    _tempo(
        "snapshot-manifest",
        *("--source-datadir", str(datadir), "--output-dir", str(archives)),
        *("--chain-id", str(chain_id)),  # reth's default is 1
        *args,
    )
    return json.loads((archives / "manifest.json").read_text())


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _download(archives: Path, datadir: Path, genesis: Path, *args: str) -> None:
    """Serve `archives` over HTTP and download them into `datadir`."""
    handler = functools.partial(_QuietHandler, directory=str(archives))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            _tempo(
                "download",
                *("--chain", str(genesis), "--datadir", str(datadir)),
                *("--manifest-url", f"http://127.0.0.1:{server.server_port}/manifest.json"),
                "--archive",  # every component, no interactive picker
                *args,
            )
        finally:
            server.shutdown()


def test_a_downloaded_snapshot_keeps_the_chain(tmp_path):
    node = dev_node(tmp_path)
    try:
        last = Web3(Web3.HTTPProvider(node.start().wait_for_rpc(want_block=30).rpc_url)).eth.get_block("latest")
    finally:
        node.stop()

    tip = _snapshot(node.datadir, tmp_path / "snapshot", node.chain_id, "--skip-consensus")["block"]
    assert tip >= last["number"], f"snapshot ends at {tip}, the node had served {last['number']}"
    _download(tmp_path / "snapshot", tmp_path / "restored", node.genesis, "--skip-consensus")

    restored = TempoNode(
        datadir=tmp_path / "restored", log_path=tmp_path / "restored.log", genesis=node.genesis, http_port=free_port()
    )
    try:
        w3 = Web3(Web3.HTTPProvider(restored.start().wait_for_rpc(want_block=tip + 1).rpc_url))
        assert w3.eth.get_block(last["number"])["hash"] == last["hash"]
    finally:
        restored.stop()


@pytest.mark.consensus
def test_a_restored_follower_resumes_from_the_snapshot_anchor(consensus_net, tmp_path):
    if not hasattr(consensus_net, "node_ws_url"):
        pytest.skip("needs the supervisord localnet (--consensus)")
    genesis = consensus_net.data_dir / "genesis.json"
    # Validators advertise their NAT address but listen on loopback.
    peers = [
        re.sub(
            r"@[^:]+:",
            "@127.0.0.1:",
            Web3(Web3.HTTPProvider(consensus_net.node_rpc_url(v.moniker))).geth.admin.node_info()["enode"],
        )
        for v in consensus_net.config.validators
    ]

    def follower(name: str) -> FollowerNode:
        return FollowerNode(
            upstream=consensus_net.node_ws_url("node0"),
            trusted_peers=peers,
            datadir=tmp_path / name,
            log_path=tmp_path / f"{name}.log",
            genesis=genesis,
            http_port=free_port(),
        )

    source = follower("source")
    try:
        target = max(poll_height(consensus_net.node_rpc_url("node0")), 30)
        w3 = Web3(Web3.HTTPProvider(source.start().wait_for_rpc(timeout=120, want_block=target).rpc_url))
        # The finalized marker the consensus half anchors on lands after the payloads.
        deadline = time.time() + 60
        while w3.eth.get_block("finalized")["number"] < target and time.time() < deadline:
            time.sleep(0.5)
        last = w3.eth.get_block("latest")
    finally:
        source.stop()

    manifest = _snapshot(source.datadir, tmp_path / "snapshot", source.chain_id, "--chain", str(genesis))
    assert manifest["block"] >= last["number"], f"snapshot ends at {manifest['block']}, had {last['number']}"
    _download(tmp_path / "snapshot", tmp_path / "restored", genesis)

    restored = follower("restored")
    try:
        w3 = Web3(
            Web3.HTTPProvider(restored.start().wait_for_rpc(timeout=120, want_block=manifest["block"] + 5).rpc_url)
        )
        assert w3.eth.get_block(last["number"])["hash"] == last["hash"]
    finally:
        restored.stop()
    floor = re.search(r"selected finalized startup range floor_height=(\d+)", node_log(restored))
    assert floor and int(floor[1]) == manifest["consensus"]["anchor_finalization_height"], (
        "did not start from the anchor"
    )
