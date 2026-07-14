"""Consensus RPC namespace (consensus_*) against the multi-validator localnet (--consensus)."""

import asyncio
import time

import pytest
from hexbytes import HexBytes
from web3 import Web3

from .utils import wait_for_block

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
    # Blocks 1 and 2, not the head pair: an epoch-boundary block carries the DKG outcome in
    # extraData (hundreds of bytes), which trips web3.py's 32-byte validation middleware.
    await wait_for_block(consensus_w3, 2)

    parent = await consensus_w3.eth.get_block(1)
    child = await consensus_w3.eth.get_block(2)
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


def _max_faults(n: int) -> int:
    """Validators that can be offline while consensus keeps quorum (BFT: n = 3f + 1)."""
    return (n - 1) // 3


# Runs last (after the RPC tests) and restores the network, since it disrupts the shared localnet.
def test_chain_survives_validator_restart(consensus_net, num_validators):
    """Chain keeps finalizing with the max tolerable validators down, then they rejoin (P0-02)."""
    f = _max_faults(num_validators)
    if f < 1:
        pytest.skip(f"need >=4 validators to tolerate a fault (have {num_validators})")
    victims = [f"node{i}" for i in range(1, f + 1)]  # node0 stays up as the observer
    primary = consensus_net.node_rpc_url("node0")
    start = _height(primary)

    for v in victims:
        consensus_net.stop_node(v)
    assert _wait_height(primary, start + 3) >= start + 3, f"chain halted with {f} validator(s) down"
    progressed = _height(primary)

    for v in victims:
        consensus_net.start_node(v)
    rejoined = consensus_net.node_rpc_url(victims[-1])
    assert _wait_height(rejoined, progressed) >= progressed, "restarted validator did not catch up"


def test_chain_halts_without_quorum_and_recovers(consensus_net, num_validators):
    """One validator past the fault threshold halts the chain; it recovers on restart."""
    f = _max_faults(num_validators)
    if f < 1:
        pytest.skip(f"need >=4 validators to lose quorum meaningfully (have {num_validators})")
    victims = [f"node{i}" for i in range(1, f + 2)]  # f+1 down → below quorum; node0 stays
    primary = consensus_net.node_rpc_url("node0")
    for v in victims:
        consensus_net.stop_node(v)
    time.sleep(3)  # let any in-flight blocks finalize, then the height should freeze
    halted = _height(primary)
    time.sleep(8)
    assert _height(primary) == halted, "chain advanced without a quorum"

    for v in victims:
        consensus_net.start_node(v)
    assert _wait_height(primary, halted + 2) >= halted + 2, "chain did not recover after restart"


def test_full_network_failure_and_recovery(consensus_net):
    """All validators down then restarted: the chain resumes from its persisted state"""
    primary = consensus_net.node_rpc_url("node0")
    before = _height(primary)

    consensus_net.stop_all()
    consensus_net.start_all()

    # A cold restart re-forms consensus from scratch, which can take longer than
    # a single-validator rejoin, so allow extra recovery time.
    assert _wait_height(primary, before + 2, timeout=120) >= before + 2, "chain did not recover after a full restart"
