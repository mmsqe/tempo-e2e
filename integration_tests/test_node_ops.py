"""Node operations: a stopped node resumes from its persisted state on restart."""

import pytest
from web3 import Web3

from .network import dev_node, running

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
