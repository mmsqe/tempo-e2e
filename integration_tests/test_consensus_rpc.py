"""Consensus RPC namespace (consensus_*) against the multi-validator localnet (--consensus)."""

import asyncio
import time

import pytest
from hexbytes import HexBytes
from web3 import Web3

pytestmark = pytest.mark.consensus


async def test_get_latest_returns_finalized_state(consensus_w3):
    resp = await consensus_w3.provider.make_request("consensus_getLatest", [])
    assert "error" not in resp, resp.get("error")
    finalized = resp["result"]["finalized"]
    assert finalized["view"] >= 1
    assert finalized["digest"].startswith("0x")


async def test_get_finalization_latest_certifies_a_block(consensus_w3):
    # The marshal archive lags finalization slightly; poll until a certificate lands.
    resp = None
    for _ in range(15):
        resp = await consensus_w3.provider.make_request("consensus_getFinalization", ["latest"])
        if "result" in resp:
            assert int(resp["result"]["block"]["header"]["number"], 16) >= 1
            return
        await asyncio.sleep(1)
    pytest.fail(f"no finalization certificate from consensus_getFinalization: {resp}")


async def test_blocks_are_produced(consensus_w3):
    assert await consensus_w3.eth.block_number >= 1


async def test_header_embeds_consensus_context(consensus_w3):
    """TIP-1031: consensus-produced headers carry consensusContext {epoch, view,
    parentView, proposer}; genesis omits it and views link parent to child."""
    n = await consensus_w3.eth.block_number
    while n < 2:  # need a parent/child pair past genesis
        await asyncio.sleep(1)
        n = await consensus_w3.eth.block_number

    parent = await consensus_w3.eth.get_block(n - 1)
    child = await consensus_w3.eth.get_block(n)
    ctx = child["consensusContext"]

    assert ctx["view"] >= 1 and ctx["epoch"] >= 0
    assert len(HexBytes(ctx["proposer"])) == 32  # an ed25519 public key, not an EVM address
    # the embedded parentView is exactly the parent header's view
    assert ctx["parentView"] == parent["consensusContext"]["view"]
    assert ctx["view"] > ctx["parentView"]

    # genesis was not consensus-produced, so the optional field is absent (spec: MUST be None).
    # Raw request: the localnet genesis packs the validator set into extraData, which
    # trips web3.py's 32-byte extraData validation middleware on get_block.
    resp = await consensus_w3.provider.make_request("eth_getBlockByNumber", ["0x0", False])
    assert "consensusContext" not in resp["result"]


def _height(rpc_url: str) -> int:
    try:
        return Web3(Web3.HTTPProvider(rpc_url)).eth.block_number
    except Exception:  # noqa: BLE001 - node may be momentarily unreachable
        return -1


def _wait_height(rpc_url: str, target: int, timeout: float = 60.0) -> int:
    deadline = time.time() + timeout
    while _height(rpc_url) < target and time.time() < deadline:
        time.sleep(1.0)
    return _height(rpc_url)


# Runs last (after the RPC tests) and restores the network, since it disrupts the shared localnet.
def test_chain_survives_validator_restart(consensus_net):
    """Chain keeps finalizing with one validator down (3/4 quorum), and it rejoins (P0-02)."""
    cluster = consensus_net.cluster
    primary = consensus_net.rpc_url  # node0 stays up throughout
    start = _height(primary)

    cluster.stop_node("node1")
    assert _wait_height(primary, start + 3) >= start + 3, "chain halted with one validator down"
    progressed = _height(primary)

    cluster.start_node("node1")
    rejoined = cluster.node_rpc_url("node1")
    assert _wait_height(rejoined, progressed) >= progressed, "restarted validator did not catch up"


def test_chain_halts_without_quorum_and_recovers(consensus_net):
    """2 of 4 validators down (below the 3/4 quorum) halts the chain; it recovers on restart"""
    cluster = consensus_net.cluster
    primary = consensus_net.rpc_url  # node0 stays up
    cluster.stop_node("node1")
    cluster.stop_node("node2")
    time.sleep(3)  # let any in-flight blocks finalize, then the height should freeze
    halted = _height(primary)
    time.sleep(8)
    assert _height(primary) == halted, "chain advanced without a quorum"

    cluster.start_node("node1")
    cluster.start_node("node2")
    assert _wait_height(primary, halted + 2) >= halted + 2, "chain did not recover after restart"


def test_full_network_failure_and_recovery(consensus_net):
    """All validators down then restarted: the chain resumes from its persisted state"""
    cluster = consensus_net.cluster
    primary = consensus_net.rpc_url
    before = _height(primary)

    cluster.stop_all()
    cluster.start_all()

    # A cold 4-node restart re-forms consensus from scratch, which can take
    # longer than a single-validator rejoin, so allow extra recovery time.
    assert _wait_height(primary, before + 2, timeout=120) >= before + 2, "chain did not recover after a full restart"
