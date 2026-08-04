"""allegro devnet tests: genesis-embedded validator set and block timestamps.

Runs a fresh 2-validator devnet and checks that ``genesis.json`` embeds the
validator set, every node loads exactly it, consensus finalizes and stays in
sync, standard eth JSON-RPC and funding work, and block production is not
throttled by reth timestamp rejections.

Gated on the ``embedded-validators`` capability (allegro); skips elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from web3 import Web3

from .network import node_log
from .utils import cluster_fixture, rpc, rpc_all

pytestmark = pytest.mark.requires("embedded-validators")

N_VALIDATORS = 2

# A broken proposer that reuses the parent's seconds timestamp gets every
# same-second proposal rejected by reth, capping production at 1 block/s; the
# fixed proposer is limited only by consensus speed (~8 blocks/s locally).
MEASURE_SECONDS = 10
MIN_BLOCKS = 15


valset_cluster = cluster_fixture("valset", N_VALIDATORS)


def test_genesis_embeds_validators(valset_cluster):
    """genesis.json carries a top-level camelCase validator array."""
    genesis = json.loads(valset_cluster[0].genesis.read_text())
    validators = genesis.get("validators")
    assert isinstance(validators, list) and len(validators) == N_VALIDATORS
    for v in validators:
        assert {"index", "publicKey", "ingress", "egress"} <= v.keys(), v
        # ingress is ip:port; egress is an ip:port the loader reduces to its IP.
        assert ":" in v["ingress"] and ":" in v["egress"]


def test_all_nodes_load_the_full_validator_set(valset_cluster):
    """Every node logs exactly the genesis pubkeys as its validator set."""
    genesis = json.loads(valset_cluster[0].genesis.read_text())
    expected = {v["publicKey"] for v in genesis["validators"]}
    for node in valset_cluster:
        log = node_log(node)
        loaded = {line.split("public_key=")[1].split()[0] for line in log.splitlines() if "adding validator" in line}
        assert loaded == expected, f"node {node.node_index} loaded {loaded}, expected {expected}"


async def test_consensus_finalizes_and_stays_in_sync(valset_cluster):
    """Blocks advance past genesis on every node and heights track together."""
    async with rpc_all(valset_cluster) as clients:
        start = {}
        for i, w3 in enumerate(clients):
            start[i] = await w3.eth.block_number
        deadline = time.time() + 20
        while time.time() < deadline:
            heights = [await w3.eth.block_number for w3 in clients]
            if all(h > start[i] for i, h in enumerate(heights)):
                # Consensus keeps nodes within a small window of each other.
                assert max(heights) - min(heights) <= 5, heights
                return
            await asyncio.sleep(0.5)
        pytest.fail(f"nodes did not all advance past {start} (last {heights})")


async def test_standard_eth_and_funding_on_validator(valset_cluster, driver):
    """Standard eth JSON-RPC works and the plain-transfer fund path succeeds."""
    async with rpc(valset_cluster[0]) as w3:
        assert await w3.eth.chain_id == 1337
        recipient = "0x000000000000000000000000000000000000dEaD"
        before = await w3.eth.get_balance(recipient)
        await driver.fund(w3, recipient, Web3.to_wei(1, "ether"))
        assert await w3.eth.get_balance(recipient) == before + Web3.to_wei(1, "ether")


async def test_block_production_is_not_throttled(valset_cluster):
    """Blocks finalize much faster than 1/s — reth accepts every proposal."""
    async with rpc(valset_cluster[0]) as w3:
        start = await w3.eth.block_number
        await asyncio.sleep(MEASURE_SECONDS)
        produced = await w3.eth.block_number - start
        assert produced >= MIN_BLOCKS, (
            f"only {produced} blocks in {MEASURE_SECONDS}s (expected >= {MIN_BLOCKS}); "
            "reth is likely rejecting proposals over non-increasing timestamps"
        )


def test_no_payload_rejections_in_logs(valset_cluster):
    """No node ever had a proposal rejected by reth's payload builder."""
    for node in valset_cluster:
        log = node_log(node)
        rejected = [line for line in log.splitlines() if "payload builder failed" in line]
        assert not rejected, f"node {node.node_index} payload failures:\n" + "\n".join(rejected[:5])


async def test_subsecond_blocks_share_seconds_timestamps(valset_cluster):
    """Timestamps never decrease and same-second neighbours are allowed."""
    async with rpc(valset_cluster[0]) as w3:
        head = await w3.eth.block_number
        first = max(1, head - 20)
        stamps = [(await w3.eth.get_block(n))["timestamp"] for n in range(first, head + 1)]
        assert all(b >= a for a, b in zip(stamps, stamps[1:])), f"timestamps decreased: {stamps}"
        assert any(b == a for a, b in zip(stamps, stamps[1:])), f"no equal neighbours in {stamps}"


async def test_timestamps_track_wall_clock(valset_cluster):
    """Head timestamp tracks wall clock instead of racing ahead of it."""
    async with rpc(valset_cluster[0]) as w3:
        latest = await w3.eth.get_block("latest")
        now = time.time()
        assert latest["timestamp"] <= now + 2, f"timestamp {latest['timestamp']} is ahead of wall clock {now:.0f}"
        assert latest["timestamp"] >= now - 30, f"timestamp {latest['timestamp']} lags wall clock {now:.0f}"
