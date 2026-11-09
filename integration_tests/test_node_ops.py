"""Node operations: restarts, start-up flags, and the CLI's own checks."""

import re
import subprocess
import time

import pytest
from web3 import Web3

from .network import default_genesis, dev_node, patched_node, resolve_tempo_bin, running

pytestmark = pytest.mark.slow


def test_restart_resumes_from_persisted_state(tmp_path):
    with running(dev_node(tmp_path, log_name="run1.log"), want_block=5) as node:
        w3 = Web3(Web3.HTTPProvider(node.rpc_url))
        height = w3.eth.block_number
        block2_hash = w3.eth.get_block(2)["hash"]

    # Same datadir: the chain resumes with identical history instead of resetting to genesis.
    with running(dev_node(tmp_path, log_name="run2.log"), want_block=height) as node:
        w3 = Web3(Web3.HTTPProvider(node.rpc_url))
        assert w3.eth.block_number >= height
        assert w3.eth.get_block(2)["hash"] == block2_hash


def test_a_zero_consensus_duration_is_refused(tmp_path):
    """A duration flag must be positive: zero stops the node at parse time (#7941)."""
    node = dev_node(tmp_path, extra_args=["--consensus.wait-for-proposal", "0s"])
    result = subprocess.run(node.command(), capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "duration must be greater than zero" in result.stderr


def _consensus_info(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([resolve_tempo_bin(), "consensus", "info", *args], capture_output=True, text=True, timeout=60)


def test_consensus_info_refuses_a_chain_the_rpc_is_not_on(tempo):
    """``--chain`` naming another chain than the RPC serves is an error, not a warning (#8025)."""
    result = _consensus_info("--rpc-url", tempo.rpc_url, "--chain", "testnet")
    assert result.returncode != 0
    # The cause of the failure, not a line beginning ``warning:`` before some later one.
    cause = rf"^\s*--chain spec has chain id \d+ but RPC returned {tempo.chain_id}$"
    assert re.search(cause, result.stderr, re.MULTILINE), result.stderr


def test_consensus_info_needs_an_rpc_url_for_a_local_chain():
    """A chain spec file names no RPC, so ``--rpc-url`` is required rather than mainnet's assumed (#8025)."""
    result = _consensus_info("--chain", str(default_genesis()))
    assert result.returncode != 0
    assert "--chain spec has no default RPC URL; pass --rpc-url explicitly" in result.stderr


def test_a_dev_chain_builds_on_a_genesis_ahead_of_the_clock(tmp_path):
    """The dev miner stamps a block one millisecond past a parent the clock has not reached, so a genesis
    an hour ahead does not hold the chain for the hour (#8189)."""
    node = patched_node(tmp_path, default_genesis(), lambda spec: spec.update(timestamp=hex(int(time.time()) + 3600)))
    with running(node, timeout=60, want_block=3):
        w3 = Web3(Web3.HTTPProvider(node.rpc_url))
        blocks = [w3.eth.get_block(number) for number in range(4)]
        stamps = [block["timestamp"] * 1000 + int(block["timestampMillisPart"], 16) for block in blocks]
        assert [later - earlier for earlier, later in zip(stamps, stamps[1:])] == [1, 1, 1]
