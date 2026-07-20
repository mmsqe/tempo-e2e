import os
import shutil
import signal
import time

import pytest

from .docker_cluster import DockerCluster
from .utils import poll_height, wait_height

pytestmark = pytest.mark.consensus

KILL_ROUNDS = 3

STRICT_REJECTION = "strict consensus startup requires a finalized certificate archive"


def _require_fault_tolerance(num_validators: int) -> None:
    """Skip unless one validator can be down while consensus keeps quorum (n = 3f + 1)."""
    if (num_validators - 1) // 3 < 1:
        pytest.skip(f"need >=4 validators to tolerate a fault (have {num_validators})")


def _node_dir(net, moniker: str):
    return net.data_dir / moniker


def _launch_script(net, moniker: str):
    """The wrapper script the backend actually execs.

    Docker's compose command is ``docker-run.sh`` (container-relative paths);
    supervisord runs ``run.sh``.  Both are generated per node, so patching the
    wrong one silently leaves the node's flags untouched.
    """
    name = "docker-run.sh" if isinstance(net, DockerCluster) else "run.sh"
    return _node_dir(net, moniker) / name


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


def _stop_and_wipe_consensus_storage(net, moniker: str) -> int:
    """Stop the node and delete its consensus storage, leaving only EL state.

    Returns a node.log offset from after the stop: panic assertions must only
    cover the restart, not the deliberate SIGTERM (a stop that catches an
    in-flight block dispatch panics in marshal instead of exiting cleanly).
    """
    net.stop_node(moniker)
    time.sleep(1)  # let the process exit before touching its storage
    consensus_dir = _node_dir(net, moniker) / "consensus"
    assert consensus_dir.is_dir(), f"expected consensus storage at {consensus_dir}"
    try:
        shutil.rmtree(consensus_dir)
    except PermissionError:
        _ensure_started(net, moniker)
        pytest.skip("consensus storage not writable from the host (docker-owned files)")
    return _log_size(net, moniker)


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


def test_rejoin_after_consensus_storage_loss(consensus_net, num_validators):
    """A validator with only EL state (no finalization certificates) rejoins
    under today's non-strict startup."""
    _require_fault_tolerance(num_validators)
    victim = f"node{num_validators - 1}"
    primary = consensus_net.node_rpc_url("node0")
    victim_rpc = consensus_net.node_rpc_url(victim)

    log_mark = _stop_and_wipe_consensus_storage(consensus_net, victim)

    _ensure_started(consensus_net, victim)
    target = poll_height(primary) + 2
    assert wait_height(victim_rpc, target, timeout=120) >= target, "validator with EL-only state failed to rejoin"
    _assert_no_panic(consensus_net, victim, log_mark)


def test_strict_startup_requires_certificates(consensus_net, num_validators):
    """With --consensus.strict-startup, the same certificate-less restore must
    be rejected — snapshots will have to bundle finalization certificates once
    strict startup becomes the default. Still opt-in as of tempo v1.10.2
    (``consensus.strict-startup`` defaults to false)."""
    _require_fault_tolerance(num_validators)
    victim = "node2"  # distinct from the other tests' victims
    primary = consensus_net.node_rpc_url("node0")
    victim_rpc = consensus_net.node_rpc_url(victim)
    run_sh = _launch_script(consensus_net, victim)
    original = run_sh.read_text()

    log_mark = _stop_and_wipe_consensus_storage(consensus_net, victim)

    run_sh.write_text(original.rstrip("\n") + " \\\n  '--consensus.strict-startup'\n")
    try:
        _ensure_started(consensus_net, victim)
        deadline = time.time() + 60
        while time.time() < deadline:
            if STRICT_REJECTION in _log_since(consensus_net, victim, log_mark):
                break
            time.sleep(1)
        else:
            pytest.fail("strict startup accepted an execution layer with no finalization certificates")
    finally:
        # Drop the flag and rejoin non-strict, leaving the cluster healthy.
        consensus_net.stop_node(victim)
        run_sh.write_text(original)
        _ensure_started(consensus_net, victim)

    target = poll_height(primary) + 2
    assert wait_height(victim_rpc, target, timeout=120) >= target, (
        "victim did not rejoin after restoring its launch script"
    )
