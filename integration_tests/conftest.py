"""Pytest fixtures and options for the tempo e2e suite."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest
import yaml
from tempo.devnet.cli import init as devnet_init
from tempo.devnet.cluster import ClusterCLI
from tempo.devnet.ports import find_free_base_ports
from tempo.devnet.supervisor import SUPERVISOR_CONFIG_FILE
from web3 import AsyncWeb3, Web3

from .network import TempoNode, free_port, resolve_tempo_bin, resolve_xtask_bin
from .utils import fund, new_account

if not os.environ.get("TMPDIR", "").startswith("/tmp"):
    os.environ["TMPDIR"] = "/tmp"
    tempfile.tempdir = "/tmp"


def pytest_addoption(parser):
    group = parser.getgroup("tempo")
    group.addoption("--tempo-bin", default=None, help="Path to the tempo node binary")
    group.addoption("--keep-data", action="store_true", default=False, help="Keep the node datadir after the run")
    group.addoption(
        "--consensus",
        action="store_true",
        default=False,
        help="Launch the multi-validator consensus localnet for consensus-marked tests",
    )


@pytest.fixture(scope="session")
def tempo(request, tmp_path_factory):
    """A locally launched tempo dev node, torn down at the end of the session."""
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("tempo")
    node = TempoNode(datadir=base / "data", log_path=base / "tempo.log", http_port=free_port())
    try:
        node.start().wait_for_rpc()
        yield node
    finally:
        node.stop()
        if not request.config.getoption("--keep-data"):
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def w3(tempo):
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(tempo.rpc_url))
    yield client
    await client.provider.disconnect()


@pytest.fixture
def chain_id(tempo) -> int:
    return tempo.chain_id


@pytest.fixture
def account():
    return new_account()


@pytest.fixture
async def funded_account(w3):
    acct = new_account()
    await fund(w3, acct.address)
    return acct


# -- consensus localnet lifecycle (client-side; tempo-devnet only does init) --


def _start_supervisord(cluster: ClusterCLI) -> subprocess.Popen:
    """Launch supervisord (nodaemon) for the cluster as a child process."""
    ini = cluster.data_dir / SUPERVISOR_CONFIG_FILE
    log = open(cluster.data_dir / "supervisord.out", "a")
    return subprocess.Popen(
        [sys.executable, "-m", "supervisor.supervisord", "-c", str(ini)],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _wait_all_running(cluster: ClusterCLI, timeout: float = 30.0) -> None:
    """Wait for the control socket, then for every node to launch."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if all(p["statename"] in ("RUNNING", "STARTING") for p in cluster.status()):
                return
        except Exception:  # noqa: BLE001 - supervisord still booting
            pass
        time.sleep(0.5)
    raise RuntimeError(f"supervisord did not start all nodes; see {cluster.data_dir}")


def _wait_for_finalization(cluster: ClusterCLI, timeout: float = 120.0) -> None:
    """Wait until node0 finalizes a block via consensus (not just produces one)."""
    w3 = Web3(Web3.HTTPProvider(cluster.node_rpc_url("node0")))
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        crashed = [p["name"] for p in cluster.status() if p["statename"] in ("FATAL", "EXITED")]
        if crashed:
            raise RuntimeError(f"validators crashed: {crashed}; see {cluster.data_dir}")
        try:
            finalized = (w3.provider.make_request("consensus_getLatest", []).get("result") or {}).get("finalized")
            if finalized and finalized.get("view", 0) >= 1 and w3.eth.block_number >= 1:
                return
        except Exception as e:  # noqa: BLE001 - consensus warming up
            last_err = e
        time.sleep(1.0)
    raise TimeoutError(f"consensus did not finalize within {timeout}s (last error: {last_err})")


def _shutdown(cluster: ClusterCLI, proc: subprocess.Popen | None) -> None:
    """Stop all nodes and supervisord itself; reap the child."""
    if proc is None:
        return
    try:
        cluster.supervisor.shutdown()
    except Exception:  # noqa: BLE001 - socket already gone
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def consensus_net(request, tmp_path_factory):
    """A 4-validator consensus localnet (opt-in via --consensus), run by tempo-devnet.

    Yields the ``ClusterCLI`` for the cluster; tests address nodes by moniker.
    """
    if not request.config.getoption("--consensus"):
        pytest.skip("consensus localnet not requested (pass --consensus)")
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("consensus")
    # Random free base ports (baked into genesis) so runs don't collide with each other or a dev node.
    config = {
        "chain_id": 1337,
        "accounts": 200,
        "epoch_length": 100,
        "seed": 0,
        "tempo_bin": resolve_tempo_bin(),
        "tempo_xtask_bin": resolve_xtask_bin(),
        "validators": [
            {"host": "127.0.0.1", "port": port, "moniker": f"node{i}"} for i, port in enumerate(find_free_base_ports(4))
        ],
    }
    config_path = base / "devnet.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False))
    data_dir = base / "data"
    try:
        devnet_init(data=str(data_dir), config=str(config_path), force=True)
    except SystemExit as e:  # devnet_init exits on failure; surface it to pytest
        raise RuntimeError(f"tempo-devnet init failed (exit {e.code}); see {base}") from e

    cluster = ClusterCLI(data_dir)
    proc: subprocess.Popen | None = None
    try:
        last_err: Exception | None = None
        for _ in range(5):  # retry: a freshly-picked port can be grabbed before launch
            try:
                proc = _start_supervisord(cluster)
                _wait_all_running(cluster)
                _wait_for_finalization(cluster)
                break
            except (RuntimeError, TimeoutError) as e:
                last_err = e
                _shutdown(cluster, proc)
                time.sleep(5)
        else:
            raise last_err
        yield cluster
    finally:
        _shutdown(cluster, proc)
        if not request.config.getoption("--keep-data"):
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def consensus_w3(consensus_net):
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(consensus_net.node_rpc_url("node0")))
    yield client
    await client.provider.disconnect()
