import os
import shutil
import signal
import time

import pytest

from .docker_cluster import DockerCluster
from .utils import poll_height, wait_height

pytestmark = pytest.mark.consensus

KILL_ROUNDS = 3

# Startup says this when the execution layer has state the certificates cannot account for.
STRICT_REJECTION = "consensus startup requires a finalized certificate archive"


def _require_fault_tolerance(num_validators: int) -> None:
    """Skip unless one validator can be down while consensus keeps quorum (n = 3f + 1)."""
    if (num_validators - 1) // 3 < 1:
        pytest.skip(f"need >=4 validators to tolerate a fault (have {num_validators})")


def _log_size(net, moniker: str) -> int:
    log = net.data_dir / moniker / "node.log"
    return log.stat().st_size if log.exists() else 0


def _log_since(net, moniker: str, since: int) -> str:
    log = net.data_dir / moniker / "node.log"
    if not log.exists():
        return ""
    with open(log, "rb") as fp:
        fp.seek(since)
        return fp.read().decode(errors="replace")


def _kill_node(net, moniker: str) -> None:
    """SIGKILL the node process — an unclean shutdown, unlike stop_node."""
    if isinstance(net, DockerCluster):
        net._run("kill", "-s", "SIGKILL", moniker)
        return
    pid = {p["name"]: p for p in net.status()}[moniker]["pid"]
    os.kill(pid, signal.SIGKILL)  # run.sh execs tempo, so this pid is the node


def _ensure_started(net, moniker: str) -> None:
    """Start the node, tolerating supervisord's autorestart having beaten us to it."""
    try:
        net.start_node(moniker)
    except Exception:
        pass


def _stop_and_take_consensus_storage(net, moniker: str):
    """Stop the node and move (not delete: the cluster is shared) its consensus storage aside. The
    returned log offset skips the stop, whose SIGTERM can panic marshal mid-dispatch."""
    net.stop_node(moniker)
    time.sleep(1)  # let the process exit before touching its storage
    consensus_dir = net.data_dir / moniker / "consensus"
    assert consensus_dir.is_dir(), f"expected consensus storage at {consensus_dir}"
    saved = consensus_dir.with_name("consensus.saved")
    try:
        shutil.rmtree(saved, ignore_errors=True)
        shutil.move(str(consensus_dir), str(saved))
    except OSError:
        _ensure_started(net, moniker)
        pytest.skip("consensus storage not writable from the host (docker-owned files)")
    return _log_size(net, moniker), saved


def _wait_for_log(net, moniker: str, since: int, needle: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in _log_since(net, moniker, since):
            return True
        time.sleep(1)
    return False


def test_unclean_kill_recovers(consensus_net, num_validators):
    """A SIGKILLed validator must reopen its journals after a torn write and catch up, not panic."""
    _require_fault_tolerance(num_validators)
    victim = "node1"  # node0 stays up as the observer
    primary = consensus_net.node_rpc_url("node0")
    victim_rpc = consensus_net.node_rpc_url(victim)
    log_mark = _log_size(consensus_net, victim)

    for _ in range(KILL_ROUNDS):
        start = poll_height(primary)
        # The victim must be live and writing journals when the kill lands.
        assert wait_height(victim_rpc, max(start, 1)) >= max(start, 1), "victim not live before kill"
        _kill_node(consensus_net, victim)

        assert wait_height(primary, start + 2) >= start + 2, "chain halted after a single unclean kill"

        _ensure_started(consensus_net, victim)
        target = poll_height(primary)
        assert wait_height(victim_rpc, target, timeout=120) >= target, "victim did not catch up after unclean kill"

    assert "panicked at" not in _log_since(consensus_net, victim, log_mark), f"{victim} panicked; see its node.log"


def test_certificate_less_restart_is_refused(consensus_net, num_validators):
    """A validator with its EL state but no finalization certificates refuses to start, then rejoins."""
    _require_fault_tolerance(num_validators)
    victim = f"node{num_validators - 1}"
    primary = consensus_net.node_rpc_url("node0")
    victim_rpc = consensus_net.node_rpc_url(victim)

    log_mark, saved = _stop_and_take_consensus_storage(consensus_net, victim)
    try:
        _ensure_started(consensus_net, victim)
        assert _wait_for_log(consensus_net, victim, log_mark, STRICT_REJECTION), (
            "startup accepted an execution layer with no finalization certificates"
        )
    finally:
        consensus_net.stop_node(victim)
        time.sleep(1)
        # Put the saved storage back, replacing whatever the refused start left.
        consensus_dir = consensus_net.data_dir / victim / "consensus"
        shutil.rmtree(consensus_dir, ignore_errors=True)
        shutil.move(str(saved), str(consensus_dir))
        _ensure_started(consensus_net, victim)

    # The cluster is shared, so this has to hand back every validator it was given.
    target = poll_height(primary) + 2
    assert wait_height(victim_rpc, target, timeout=120) >= target, (
        "victim did not rejoin once its certificates were back"
    )
