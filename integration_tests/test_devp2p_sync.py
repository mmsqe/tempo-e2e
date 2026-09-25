"""Optional P2P sync: ``--consensus.devp2p.finalizations`` gossips finalization certificates over
devp2p (the ``tempo/1`` subprotocol), so a follower keeps up from its peers when its upstream RPC
is gone, backlog included."""

import re
import subprocess

import pytest
from tempo.devnet.ports import find_free_base_ports
from web3 import Web3

from .conftest import _consensus_net_supervisord, _run_devnet_init
from .network import FollowerNode, free_port, resolve_tempo_bin, resolve_xtask_bin
from .utils import poll_height, wait_height

pytestmark = [pytest.mark.tempo, pytest.mark.consensus, pytest.mark.slow]

GOSSIP = "--consensus.devp2p.finalizations"
DEAD_UPSTREAM = "ws://127.0.0.1:9"  # the discard port refuses, so only devp2p can bring blocks
VALIDATORS = 4


@pytest.fixture(scope="module")
def gossip_net(request, tmp_path_factory):
    """Four validators gossiping their finalizations."""
    if not request.config.getoption("--consensus"):
        pytest.skip("consensus localnet not requested (pass --consensus)")
    if GOSSIP not in subprocess.run([resolve_tempo_bin(), "node", "--help"], capture_output=True, text=True).stdout:
        pytest.skip(f"this tempo has no {GOSSIP}")
    base = tmp_path_factory.mktemp("gossip-net")
    config = {
        "chain_id": 1337,
        "accounts": 20,
        "seed": 0,
        "patch_node_flags": [GOSSIP],
        "tempo_bin": resolve_tempo_bin(),
        "tempo_xtask_bin": resolve_xtask_bin(),
        "validators": [
            {"host": "127.0.0.1", "port": port, "moniker": f"node{i}"}
            for i, port in enumerate(find_free_base_ports(VALIDATORS))
        ],
    }
    yield from _consensus_net_supervisord(request, base, _run_devnet_init(base, config, gen_compose_file=False))


def _follower(cluster, base, *, gossip: bool) -> FollowerNode:
    """A follower peered with every validator over devp2p, whose upstream websocket is dead."""
    enodes = [
        Web3(Web3.HTTPProvider(cluster.node_rpc_url(validator.moniker))).geth.admin.node_info()["enode"]
        for validator in cluster.config.validators
    ]
    return FollowerNode(
        upstream=DEAD_UPSTREAM,
        # The advertised host is whatever NAT resolution found; the validators listen on loopback.
        trusted_peers=[re.sub(r"@[^:]+:", "@127.0.0.1:", enode) for enode in enodes],
        datadir=base / "follower",
        log_path=base / "follower.log",
        genesis=cluster.data_dir / "genesis.json",
        http_port=free_port(),
        extra_args=[GOSSIP] if gossip else [],
    )


def test_a_follower_syncs_over_devp2p_when_its_upstream_is_dead(gossip_net, tmp_path):
    follower = _follower(gossip_net, tmp_path, gossip=True).start()
    try:
        # Past the validators' current height: certificates keep arriving, not just the backlog.
        follower.wait_for_rpc(timeout=120, want_block=poll_height(gossip_net.node_rpc_url("node0")) + 5)
    finally:
        follower.stop()


def test_without_the_gossip_a_dead_upstream_leaves_a_follower_at_genesis(gossip_net, tmp_path):
    follower = _follower(gossip_net, tmp_path, gossip=False).start()
    try:
        follower.wait_for_rpc(timeout=60, want_block=0)
        validator = gossip_net.node_rpc_url("node0")
        wait_height(validator, poll_height(validator) + 5)
        assert poll_height(follower.rpc_url) == 0
    finally:
        follower.stop()


def test_a_late_follower_recovers_the_backlog_over_devp2p(gossip_net, tmp_path):
    """Blocks the follower never saw announced come from its peers too. Now and then a follower has
    exited doing this, its log ending with the genesis header refused as a payload."""
    validator = gossip_net.node_rpc_url("node0")
    backlog = wait_height(validator, poll_height(validator) + 20)
    follower = _follower(gossip_net, tmp_path, gossip=True).start()
    try:
        follower.wait_for_rpc(timeout=120, want_block=backlog + 5)
    finally:
        follower.stop()
