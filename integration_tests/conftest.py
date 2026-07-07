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

from .docker_cluster import DockerCluster
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
    group.addoption(
        "--consensus-docker",
        action="store_true",
        default=False,
        help="Run the consensus localnet in Docker containers (docker compose) instead of supervisord",
    )
    group.addoption(
        "--tempo-image",
        default=os.environ.get("TEMPO_IMAGE", "tempo:latest"),
        help="Docker image for validator containers in --consensus-docker mode",
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


def _wait_for_finalization(cluster, timeout: float = 120.0) -> None:
    """Wait until node0 finalizes a block via consensus (not just produces one).

    Works for both the supervisord ``ClusterCLI`` and the ``DockerCluster``:
    both expose ``node_rpc_url`` and a crash-listing method (``_crashed_nodes``).
    """
    w3 = Web3(Web3.HTTPProvider(cluster.node_rpc_url("node0")))
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        crashed = _crashed_nodes(cluster)
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


def _crashed_nodes(cluster) -> list[str]:
    """List nodes that have died, for either cluster backend."""
    if isinstance(cluster, DockerCluster):
        return cluster.crashed()
    return [p["name"] for p in cluster.status() if p["statename"] in ("FATAL", "EXITED")]


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


def _init_consensus_devnet(request, base, *, docker: bool):
    """Build a 4-validator devnet config and run ``tempo-devnet init``.

    Returns the resolved ``data_dir``.  In Docker mode the node binary lives in
    the image (``tempo_bin`` stays the in-image name) and a ``docker-compose.yml``
    is generated; ``tempo-xtask`` always runs on the host to build genesis + keys.
    """
    # Random free base ports (baked into genesis) so runs don't collide with each other or a dev node.
    config = {
        "chain_id": 1337,
        "accounts": 200,
        "epoch_length": 100,
        "seed": 0,
        # Docker runs the in-image `tempo`; supervisord runs the host binary.
        "tempo_bin": "tempo" if docker else resolve_tempo_bin(),
        "tempo_xtask_bin": resolve_xtask_bin(),
        "validators": [
            {"host": "127.0.0.1", "port": port, "moniker": f"node{i}"} for i, port in enumerate(find_free_base_ports(4))
        ],
    }
    if docker:
        config["docker"] = {"image": request.config.getoption("--tempo-image"), "network": "tempo-devnet"}
    config_path = base / "devnet.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False))
    data_dir = base / "data"
    try:
        devnet_init(data=str(data_dir), config=str(config_path), force=True, gen_compose_file=docker)
    except SystemExit as e:  # devnet_init exits on failure; surface it to pytest
        raise RuntimeError(f"tempo-devnet init failed (exit {e.code}); see {base}") from e
    return data_dir


@pytest.fixture(scope="session")
def consensus_net(request, tmp_path_factory):
    """A 4-validator consensus localnet, run by tempo-devnet.

    Opt-in: ``--consensus`` (supervisord) or ``--consensus-docker`` (containers).
    Yields a cluster handle (``ClusterCLI`` or ``DockerCluster``); both expose
    ``node_rpc_url`` plus ``start_node``/``stop_node``/``start_all``/``stop_all``,
    so the consensus tests are identical across both backends.
    """
    docker = request.config.getoption("--consensus-docker")
    if not (docker or request.config.getoption("--consensus")):
        pytest.skip("consensus localnet not requested (pass --consensus or --consensus-docker)")
    if docker:
        if shutil.which("docker") is None:
            pytest.skip("--consensus-docker requested but the docker CLI is not available")
        # Preflight the image before doing any work: docker would otherwise try to
        # pull an unknown local tag from a registry and fail with an opaque error.
        image = request.config.getoption("--tempo-image")
        if not _docker_image_exists(image):
            pytest.skip(
                f"--consensus-docker: image {image!r} not found locally. Build it first, e.g. "
                f"`docker build -t {image} ../tempo` (or point --tempo-image/$TEMPO_IMAGE at an existing image)."
            )
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("consensus-docker" if docker else "consensus")
    data_dir = _init_consensus_devnet(request, base, docker=docker)

    if docker:
        yield from _consensus_net_docker(request, base, data_dir)
    else:
        yield from _consensus_net_supervisord(request, base, data_dir)


def _consensus_net_supervisord(request, base, data_dir):
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


def _docker_image_exists(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True).returncode == 0


def _consensus_net_docker(request, base, data_dir):
    cluster = DockerCluster(data_dir)
    try:
        try:
            cluster.up()
            _wait_for_finalization(cluster)
        except (RuntimeError, TimeoutError, subprocess.CalledProcessError) as e:
            # Surface docker's own stderr (e.g. compose/up failures) and any
            # container logs — otherwise the real reason is swallowed.
            detail = ""
            if isinstance(e, subprocess.CalledProcessError) and e.stderr:
                detail += f"\n[compose stderr]\n{e.stderr.strip()}"
            logs = cluster.logs(tail=40)
            if logs.strip():
                detail += f"\n[container logs]\n{logs.strip()}"
            cluster.down()
            raise RuntimeError(f"docker consensus localnet failed to start/finalize:{detail or ' (no output)'}") from e
        yield cluster
    finally:
        cluster.down()
        if not request.config.getoption("--keep-data"):
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def consensus_w3(consensus_net):
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(consensus_net.node_rpc_url("node0")))
    yield client
    await client.provider.disconnect()
