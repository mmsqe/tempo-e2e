"""Public-node topology (--consensus-docker).

validators --WS--> follower0 --WS--> public0 --P2P--> proxy0

Reads sync down; a tx submitted to public0 gossips back up to a validator and mines.
"""

import time

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import Web3
from web3.exceptions import TimeExhausted

from .conftest import TWO_NET_FOLLOWER, TWO_NET_PROXY, TWO_NET_PUBLIC
from .network import FAUCET_PRIVATE_KEY
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


def _wait_for_peers(rpc_url: str, timeout: float = 60.0) -> int:
    """Poll until the node has at least one devp2p peer; return the final count."""
    deadline = time.time() + timeout
    while _peer_count(rpc_url) < 1 and time.time() < deadline:
        time.sleep(1.0)
    return _peer_count(rpc_url)


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


def _assert_peers_with_proxy(cluster, moniker: str) -> None:
    """Assert ``moniker`` joins devp2p and one of its peers is the P2P proxy (by enode)."""
    rpc = cluster.node_rpc_url(moniker)
    assert _wait_for_peers(rpc) >= 1, f"{moniker} did not establish a devp2p peer"

    proxy_id = (cluster.data_dir / TWO_NET_PROXY / "enode.identity").read_text().strip()
    peers = Web3(Web3.HTTPProvider(rpc)).provider.make_request("admin_peers", [])["result"] or []
    assert any(proxy_id in p.get("enode", "") for p in peers), (
        f"{moniker}'s peers do not include proxy {TWO_NET_PROXY!r} (enode {proxy_id[:16]}…)"
    )


def test_public_node_peers_with_proxy(two_network_net):
    """The public node peers with the P2P proxy (its only trusted peer, by enode)."""
    _assert_peers_with_proxy(two_network_net, TWO_NET_PUBLIC)


def test_follower_peers_with_proxy(two_network_net):
    """The follower joins devp2p and peers with the P2P proxy by enode."""
    _assert_peers_with_proxy(two_network_net, TWO_NET_FOLLOWER)


def _send_stablecoin_transfer(rpc_url: str, recipient: str, amount: int = 4321):
    """Submit a type-2 PATH_USD transfer from the prefunded faucet key; return the tx hash.

    Uses a fixed ``maxFeePerGas`` rather than ``eth.get_block`` — tempo packs the
    validator set into a block's ``extraData``, which trips web3.py's PoA check.
    """
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    acct = Account.from_key(FAUCET_PRIVATE_KEY)
    tx = {
        "to": PATH_USD,
        "data": ERC20.fns.transfer(recipient, amount).data,
        "value": 0,
        "nonce": w3.eth.get_transaction_count(acct.address),
        "chainId": w3.eth.chain_id,
        "gas": 1_000_000,
        "maxFeePerGas": 100_000_000_000,
        "maxPriorityFeePerGas": 2_000_000_000,
        "type": 2,
    }
    return w3.eth.send_raw_transaction(Account.sign_transaction(tx, acct.key).raw_transaction)


def test_send_tx_via_public_node(two_network_net):
    """A tx submitted to the public node's RPC gossips to a validator and is mined.

    Path: public0 -> follower0 -> validator (devp2p tx gossip). Proves the
    read-only public side can accept writes without exposing the validators.
    """
    public = two_network_net.node_rpc_url(TWO_NET_PUBLIC)

    # Need a devp2p peer (the follower) before the tx has a gossip path upstream.
    assert _wait_for_peers(public) >= 1, "public node has no devp2p peer to gossip the tx to"

    recipient = Account.create().address
    tx_hash = _send_stablecoin_transfer(public, recipient)

    try:
        receipt = Web3(Web3.HTTPProvider(public)).eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    except TimeExhausted:
        receipt = None
    assert receipt is not None, "tx submitted to the public node was never mined (no gossip path to validators)"
    assert receipt["status"] == 1, f"tx reverted: {receipt}"
