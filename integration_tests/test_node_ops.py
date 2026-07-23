"""Node operations: a stopped node resumes from its persisted state on restart."""

import time

import pytest
from web3 import Web3

from .network import dev_node

pytestmark = pytest.mark.slow


def _wait_height(rpc_url: str, target: int, timeout: float = 60.0) -> int:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if w3.eth.block_number >= target:
                return w3.eth.block_number
        except Exception:  # noqa: BLE001 - RPC warming up
            pass
        time.sleep(0.5)
    raise TimeoutError(f"height {target} not reached on {rpc_url}")


def test_restart_resumes_from_persisted_state(tmp_path):
    node = dev_node(tmp_path, log_name="run1.log")
    node.start().wait_for_rpc()
    _wait_height(node.rpc_url, 5)
    w3 = Web3(Web3.HTTPProvider(node.rpc_url))
    height = w3.eth.block_number
    block2_hash = w3.eth.get_block(2)["hash"]
    node.stop()

    # Restart on the same datadir: the chain resumes past the persisted tip with
    # identical history, rather than resetting to genesis.
    node2 = dev_node(tmp_path, log_name="run2.log")
    node2.start().wait_for_rpc(want_block=height)
    try:
        w3b = Web3(Web3.HTTPProvider(node2.rpc_url))
        assert w3b.eth.block_number >= height
        assert w3b.eth.get_block(2)["hash"] == block2_hash
    finally:
        node2.stop()
