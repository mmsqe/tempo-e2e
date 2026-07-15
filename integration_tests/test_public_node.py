"""Public-node topology against the two-network localnet (--consensus-docker).

    validators --WS--> follower0 --WS--> public0

Public node syncs by following follower, these tests assert it receives finalized chain and stays on same fork.
"""

import pytest
from web3 import Web3

from .conftest import TWO_NET_FOLLOWER, TWO_NET_PUBLIC
from .utils import poll_height, wait_height

pytestmark = pytest.mark.consensus


def _block_hash(rpc_url: str, number: int) -> str:
    return Web3(Web3.HTTPProvider(rpc_url)).eth.get_block(number)["hash"].to_0x_hex()


def test_public_node_syncs_blocks(two_network_net):
    """The public node advances past genesis and keeps following new blocks."""
    public = two_network_net.node_rpc_url(TWO_NET_PUBLIC)

    start = wait_height(public, 1)
    assert start >= 1, "public node never synced its first block from the follower"

    # It keeps up with the chain, not just a one-off block.
    later = wait_height(public, start + 3)
    assert later >= start + 3, f"public node stopped syncing (stuck at {later}, wanted >= {start + 3})"


def test_public_node_matches_follower_chain(two_network_net):
    """The public node is on the same fork as the follower it syncs from."""
    follower = two_network_net.node_rpc_url(TWO_NET_FOLLOWER)
    public = two_network_net.node_rpc_url(TWO_NET_PUBLIC)

    # Compare a block both have finalized (a few below the follower's head, so the
    # public node has certainly received it and neither side is mid-produce).
    target = max(1, poll_height(follower) - 2)
    assert wait_height(public, target) >= target, "public node did not catch up to the follower"

    assert _block_hash(public, target) == _block_hash(follower, target), (
        f"public node and follower disagree on block {target} (different fork)"
    )
