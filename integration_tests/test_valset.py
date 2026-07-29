"""Genesis-embedded validator set (allegro's ``valset`` feature).

Runs a fresh 2-validator devnet and checks that ``genesis.json`` embeds the
validator set, every node loads exactly it, consensus finalizes and stays in
sync, and standard eth JSON-RPC plus the plain-transfer fund path work.

Gated on the ``embedded-validators`` capability (allegro); skips elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from web3 import AsyncWeb3, Web3

pytestmark = pytest.mark.requires("embedded-validators")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

N_VALIDATORS = 2


@pytest.fixture(scope="module")
def valset_cluster(request, driver, tmp_path_factory):
    """A freshly generated ``N_VALIDATORS`` devnet, started and finalizing."""
    if not hasattr(driver, "make_cluster"):
        pytest.skip(f"backend {driver.name!r} has no make_cluster()")
    base = tmp_path_factory.mktemp("valset")
    nodes = driver.make_cluster(base, N_VALIDATORS)
    try:
        for node in nodes:
            node.start()
        for node in nodes:
            node.wait_for_rpc(timeout=90.0, want_block=1)
        yield nodes
    finally:
        for node in nodes:
            node.stop()
        if request.config.getoption("--clean-data"):
            import shutil

            shutil.rmtree(base, ignore_errors=True)


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
        log = _ANSI.sub("", node.log_path.read_text())
        loaded = {line.split("public_key=")[1].split()[0] for line in log.splitlines() if "adding validator" in line}
        assert loaded == expected, f"node {node.node_index} loaded {loaded}, expected {expected}"


async def test_consensus_finalizes_and_stays_in_sync(valset_cluster):
    """Blocks advance past genesis on every node and heights track together."""
    clients = [AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(n.rpc_url)) for n in valset_cluster]
    try:
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
    finally:
        for w3 in clients:
            await w3.provider.disconnect()


async def test_standard_eth_and_funding_on_validator(valset_cluster, driver):
    """Standard eth JSON-RPC works and the plain-transfer fund path succeeds."""
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(valset_cluster[0].rpc_url))
    try:
        assert await w3.eth.chain_id == 1337
        recipient = "0x000000000000000000000000000000000000dEaD"
        before = await w3.eth.get_balance(recipient)
        await driver.fund(w3, recipient, Web3.to_wei(1, "ether"))
        assert await w3.eth.get_balance(recipient) == before + Web3.to_wei(1, "ether")
    finally:
        await w3.provider.disconnect()
