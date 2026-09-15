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


def _node_dir(net, moniker: str):
    return net.data_dir / moniker


def _log_size(net, moniker: str) -> int:
    log = _node_dir(net, moniker) / "node.log"
    return log.stat().st_size if log.exists() else 0


def _log_since(net, moniker: str, since: int) -> str:
    log = _node_dir(net, moniker) / "node.log"
    if not log.exists():
        return ""
    with open(log, "rb") as fp:
        fp.seek(since)
        return fp.read().decode(errors="replace")


def _assert_no_panic(net, moniker: str, since: int) -> None:
    assert "panicked at" not in _log_since(net, moniker, since), f"{moniker} panicked; see its node.log"


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
    """Stop the node and move its consensus storage aside, leaving only EL state.

    Moved, not deleted: the cluster is shared, and a validator that cannot start is one short
    of quorum for every test after this one. The returned node.log offset starts after the
    stop, so panic assertions skip the deliberate SIGTERM, which panics in marshal when it
    catches an in-flight block dispatch.
    """
    net.stop_node(moniker)
    time.sleep(1)  # let the process exit before touching its storage
    consensus_dir = _node_dir(net, moniker) / "consensus"
    assert consensus_dir.is_dir(), f"expected consensus storage at {consensus_dir}"
    saved = consensus_dir.with_name("consensus.saved")
    try:
        shutil.rmtree(saved, ignore_errors=True)
        shutil.move(str(consensus_dir), str(saved))
    except OSError:
        _ensure_started(net, moniker)
        pytest.skip("consensus storage not writable from the host (docker-owned files)")
    return _log_size(net, moniker), saved


def _restore_consensus_storage(net, moniker: str, saved) -> None:
    """Put the saved consensus storage back, replacing whatever the refused start left."""
    consensus_dir = _node_dir(net, moniker) / "consensus"
    shutil.rmtree(consensus_dir, ignore_errors=True)
    shutil.move(str(saved), str(consensus_dir))


def _wait_for_log(net, moniker: str, since: int, needle: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in _log_since(net, moniker, since):
            return True
        time.sleep(1)
    return False


def test_unclean_kill_recovers(consensus_net, num_validators):
    """A SIGKILLed validator must reopen its journals and catch up, not panic.

    Regression cover for the ``BlobCorrupt``/"unable to open journal" panic on
    a torn journal write (need commonware bump in commonwarexyz/monorepo#4256).
    """
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

    _assert_no_panic(consensus_net, victim, log_mark)


def test_certificate_less_restart_is_refused(consensus_net, num_validators):
    """A validator that keeps its EL state but loses its finalization certificates must refuse
    to start, and must rejoin once they are back.

    Startup began requiring the certificates unconditionally with commonware 2026.7.0;
    ``--consensus.strict-startup``, which used to select this, is now a no-op that only parses.
    """
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
        _restore_consensus_storage(consensus_net, victim, saved)
        _ensure_started(consensus_net, victim)

    # The cluster is shared, so this has to hand back every validator it was given.
    target = poll_height(primary) + 2
    assert wait_height(victim_rpc, target, timeout=120) >= target, (
        "victim did not rejoin once its certificates were back"
    )
