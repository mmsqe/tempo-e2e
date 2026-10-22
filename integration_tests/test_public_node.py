"""Public-node topology (--consensus-docker): validators --WS--> follower0 --WS--> public0 --P2P--> proxy0."""

import time

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import Web3

from .conftest import TWO_NET_FOLLOWER, TWO_NET_PROXY, TWO_NET_PUBLIC
from .network import FAUCET_PRIVATE_KEY
from .utils import DEFAULT_MAX_FEE_PER_GAS, DEFAULT_MAX_PRIORITY_FEE_PER_GAS, poll_height, wait_height

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

    # A few below the follower's head, so the public node has certainly received it.
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


def test_send_tx_via_public_node(two_network_net):
    """A tx sent to public0 gossips via follower0 to a validator and is mined."""
    public = two_network_net.node_rpc_url(TWO_NET_PUBLIC)

    # Need a devp2p peer (the follower) before the tx has a gossip path upstream.
    assert _wait_for_peers(public) >= 1, "public node has no devp2p peer to gossip the tx to"

    # A type-2 transfer from the faucet key, at a fixed fee: tempo's extraData trips web3.py's PoA check.
    w3 = Web3(Web3.HTTPProvider(public))
    faucet = Account.from_key(FAUCET_PRIVATE_KEY)
    tx = {
        "to": PATH_USD,
        "data": ERC20.fns.transfer(Account.create().address, 4321).data,
        "value": 0,
        "nonce": w3.eth.get_transaction_count(faucet.address),
        "chainId": w3.eth.chain_id,
        "gas": 1_000_000,
        "maxFeePerGas": DEFAULT_MAX_FEE_PER_GAS,
        "maxPriorityFeePerGas": DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
        "type": 2,
    }
    tx_hash = w3.eth.send_raw_transaction(Account.sign_transaction(tx, faucet.key).raw_transaction)

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    assert receipt["status"] == 1, f"tx reverted: {receipt}"
