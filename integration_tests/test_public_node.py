"""Public-node topology (--consensus-docker).

validators --WS--> follower0 --WS--> public0 --P2P--> proxy0
"""

import time

import pytest
from web3 import Web3

from .conftest import TWO_NET_FOLLOWER, TWO_NET_PROXY, TWO_NET_PUBLIC
from .utils import poll_height, wait_height

pytestmark = pytest.mark.consensus


def _block_hash(rpc_url: str, number: int) -> str:
    return Web3(Web3.HTTPProvider(rpc_url)).eth.get_block(number)["hash"].to_0x_hex()


def _peer_count(rpc_url: str) -> int:
    try:
        r = Web3(Web3.HTTPProvider(rpc_url)).provider.make_request("net_peerCount", [])
        return int(r["result"], 16) if "result" in r else -1
    except Exception:
        return -1


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


def test_public_node_peers_with_proxy(two_network_net):
    """The public node peers with the P2P proxy (its only trusted peer, by enode)."""
    public = two_network_net.node_rpc_url(TWO_NET_PUBLIC)

    deadline = time.time() + 60.0
    while _peer_count(public) < 1 and time.time() < deadline:
        time.sleep(1.0)
    assert _peer_count(public) >= 1, "public node did not establish a P2P peer (proxy)"

    # Single trusted peer must be the proxy — match its enode identity.
    proxy_id = (two_network_net.data_dir / TWO_NET_PROXY / "enode.identity").read_text().strip()
    peers = Web3(Web3.HTTPProvider(public)).provider.make_request("admin_peers", [])["result"] or []
    assert any(proxy_id in p.get("enode", "") for p in peers), (
        f"public node's peer is not proxy {TWO_NET_PROXY!r} (enode {proxy_id[:16]}…)"
    )
