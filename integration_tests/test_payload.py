"""allegro payload preparation: a proposal reuses the job started on the block just accepted.

Nothing exports the hit/miss counters, so the tests read them from the node log.
"""

from __future__ import annotations

import asyncio

import pytest
from web3 import Web3

from .network import node_log
from .utils import cluster_fixture, rpc, wait_for_block

pytestmark = pytest.mark.requires("embedded-validators")

N_VALIDATORS = 2

# Long enough for both validators to lead several views each.
WARMUP_SECONDS = 6

# The prepared-payload path only shows up at debug level.
DEBUG_LOG = {"RUST_LOG": "allegro_consensus=debug,allegro_node=debug,info"}

# A node's first proposal has nothing to prepare on; allow one more for slack.
COLD_BUILD_BUDGET = 2

# Log line per count.
MARKERS = {
    "proposed": "proposed block",
    "prepared": "prepared payload job",
    "reused": "proposing prepared payload",
    "cold": "building from cold",
    "vanished": "payload job vanished",
    "prepare_failed": "failed to prepare payload",
}


payload_cluster = cluster_fixture("payload", N_VALIDATORS, env=DEBUG_LOG, warmup=WARMUP_SECONDS)


def counts(node) -> dict[str, int]:
    """How many of each payload-path event the node logged."""
    lines = node_log(node).splitlines()
    return {name: sum(1 for line in lines if text in line) for name, text in MARKERS.items()}


def test_proposals_reuse_prepared_payloads(payload_cluster):
    """Every proposal but the node's first reuses a job prepared a view early."""
    for node in payload_cluster:
        c = counts(node)
        assert c["proposed"] >= 3, f"node {node.node_index} barely proposed: {c}"
        assert c["prepared"] > 0, f"node {node.node_index} never prepared a payload: {c}"
        assert c["reused"] >= c["proposed"] - COLD_BUILD_BUDGET, (
            f"node {node.node_index} rebuilt from cold too often: {c}; "
            "leader prediction or the prepared-parent match is off"
        )


def test_prepared_payloads_are_not_wasted(payload_cluster):
    """A healthy network abandons no prepared job: every prediction lands."""
    for node in payload_cluster:
        c = counts(node)
        assert c["prepare_failed"] == 0, f"node {node.node_index} failed to start a job: {c}"
        assert c["cold"] <= COLD_BUILD_BUDGET, f"node {node.node_index} fell back to cold builds: {c}"
        assert c["vanished"] == 0, f"node {node.node_index} lost a job it had just started: {c}"


async def test_transactions_land_promptly(payload_cluster, driver):
    """reth builds until the payload is resolved, so a job started early still takes a later transaction."""
    async with rpc(payload_cluster[0]) as w3:
        recipient = Web3.to_checksum_address("0x00000000000000000000000000000000000b10c5")
        before = await w3.eth.block_number

        tx_hash = await driver.fund(w3, recipient, Web3.to_wei(1, "ether"))
        receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        assert receipt["status"] == 1, f"transfer reverted: {receipt}"

        waited = receipt["blockNumber"] - before
        assert waited <= 20, f"transaction waited {waited} blocks to be included"
        block = await w3.eth.get_block(receipt["blockNumber"])
        assert receipt["transactionHash"] in block["transactions"], "receipt block lacks the transaction"


async def test_finalized_never_regresses(payload_cluster):
    """`latest` may name a prepared block that never lands; `finalized` only moves forward, never past it."""
    async with rpc(payload_cluster[0]) as w3:
        heights = []
        for _ in range(10):
            finalized = await w3.eth.get_block("finalized")
            latest = await w3.eth.get_block("latest")
            assert finalized["number"] <= latest["number"], (
                f"finalized {finalized['number']} is ahead of latest {latest['number']}"
            )
            heights.append(finalized["number"])
            await asyncio.sleep(0.5)

        assert all(b >= a for a, b in zip(heights, heights[1:])), f"finalized went backwards: {heights}"
        assert heights[-1] > heights[0], f"finalization stalled at {heights[0]}"


async def test_speculative_head_stays_a_real_block(payload_cluster):
    """Whatever `latest` names is retrievable and linked to its predecessor."""
    for node in payload_cluster:
        async with rpc(node) as w3:
            head = await w3.eth.get_block("latest")
            assert head["number"] > 0
            parent = await w3.eth.get_block(head["number"] - 1)
            assert head["parentHash"] == parent["hash"], f"head {head['number']} does not link to its predecessor"


# Runs last: restarting a node truncates the log the tests above read.
@pytest.mark.slow
@pytest.mark.xfail(
    reason="allegro has no block backfill: a returning node can only verify parents reth had "
    "already persisted, and answers SYNCING for the rest. Passes when the timing is lucky. "
    "See node/src/finalizer.rs.",
    strict=False,
)
async def test_chain_recovers_after_a_validator_outage(payload_cluster):
    """Two validators lose quorum while one is down; on return, every parent it is asked about predates it."""
    survivor, casualty = payload_cluster[0], payload_cluster[1]
    async with rpc(survivor) as w3:
        casualty.stop()
        await asyncio.sleep(3)
        stalled = await w3.eth.block_number

        casualty.start()
        casualty.wait_for_rpc(timeout=90.0, want_block=stalled)
        await wait_for_block(w3, stalled + 1, timeout=15)
