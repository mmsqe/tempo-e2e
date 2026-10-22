"""Node operations: a stopped node resumes from its persisted state on restart."""

import pytest
from web3 import Web3

from .network import dev_node

pytestmark = pytest.mark.slow


def test_restart_resumes_from_persisted_state(tmp_path):
    node = dev_node(tmp_path, log_name="run1.log")
    try:
        w3 = Web3(Web3.HTTPProvider(node.start().wait_for_rpc(want_block=5).rpc_url))
        height = w3.eth.block_number
        block2_hash = w3.eth.get_block(2)["hash"]
    finally:
        node.stop()

    # Same datadir: the chain resumes with identical history instead of resetting to genesis.
    node2 = dev_node(tmp_path, log_name="run2.log")
    try:
        w3 = Web3(Web3.HTTPProvider(node2.start().wait_for_rpc(want_block=height).rpc_url))
        assert w3.eth.block_number >= height
        assert w3.eth.get_block(2)["hash"] == block2_hash
    finally:
        node2.stop()
