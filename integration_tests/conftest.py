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
        if request.config.getoption("--clean-data"):
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


def _run_devnet_init(base, config: dict, *, gen_compose_file: bool):
    """Write ``config`` to ``base/devnet.yaml``, run ``tempo-devnet init``, return ``data_dir``."""
    config_path = base / "devnet.yaml"
    config_path.write_text(yaml.dump(config, default_flow_style=False))
    data_dir = base / "data"
    try:
        devnet_init(data=str(data_dir), config=str(config_path), force=True, gen_compose_file=gen_compose_file)
    except SystemExit as e:  # devnet_init exits on failure; surface it to pytest
        raise RuntimeError(f"tempo-devnet init failed (exit {e.code}); see {base}") from e
    return data_dir


def _init_consensus_devnet(request, base, *, docker: bool):
    """Build a 4-validator devnet config and run ``tempo-devnet init``.

    Returns the resolved ``data_dir``.  In Docker mode the node binary lives in
    the image (``tempo_bin`` stays the in-image name) and a ``docker-compose.yml``
    is generated; ``tempo-xtask`` always runs on the host to build genesis + keys.
    """
    n = request.config.getoption("--consensus-validators")
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
            {"host": "127.0.0.1", "port": port, "moniker": f"node{i}"} for i, port in enumerate(find_free_base_ports(n))
        ],
    }
    if docker:
        config["docker"] = {"image": request.config.getoption("--tempo-image"), "network": "tempo-devnet"}
    return _run_devnet_init(base, config, gen_compose_file=docker)


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
        _skip_unless_docker_image(request)
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
        if request.config.getoption("--clean-data"):
            shutil.rmtree(base, ignore_errors=True)


def _docker_image_exists(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True).returncode == 0


def _skip_unless_docker_image(request) -> None:
    if shutil.which("docker") is None:
        pytest.skip("--consensus-docker requested but the docker CLI is not available")
    image = request.config.getoption("--tempo-image")
    if not _docker_image_exists(image):
        pytest.skip(
            f"--consensus-docker: image {image!r} not found locally. Build/pull it first "
            f"(e.g. `docker buildx bake tempo --load`), or point --tempo-image/$TEMPO_IMAGE at an existing image."
        )


def _serve_docker_cluster(request, base, data_dir, wait_ready, *, label):
    """Bring up ``data_dir``'s compose stack, wait for readiness, yield it, then tear down.

    On failure, surfaces compose stderr and recent container logs (which docker
    otherwise swallows).  Shared by the single- and two-network docker fixtures.
    """
    cluster = DockerCluster(data_dir)
    try:
        try:
            cluster.up()
            cluster.start_log_followers()  # stream each container's logs to <node>/node.log
            wait_ready(cluster)
        except (RuntimeError, TimeoutError, subprocess.CalledProcessError) as e:
            detail = ""
            if isinstance(e, subprocess.CalledProcessError) and e.stderr:
                detail += f"\n[compose stderr]\n{e.stderr.strip()}"
            logs = cluster.logs(tail=40)
            if logs.strip():
                detail += f"\n[container logs]\n{logs.strip()}"
            raise RuntimeError(f"{label} failed to start:{detail or ' (no output)'}") from e
        yield cluster
    finally:
        cluster.stop_log_followers()
        cluster.down()
        if request.config.getoption("--clean-data"):
            shutil.rmtree(base, ignore_errors=True)


def _consensus_net_docker(request, base, data_dir):
    yield from _serve_docker_cluster(request, base, data_dir, _wait_for_finalization, label="docker consensus localnet")


@pytest.fixture(scope="session")
def num_validators(request) -> int:
    """Validator count of the consensus localnet (see ``--consensus-validators``)."""
    return request.config.getoption("--consensus-validators")


@pytest.fixture
async def consensus_w3(consensus_net):
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(consensus_net.node_rpc_url("node0")))
    yield client
    await client.provider.disconnect()


TWO_NET_FOLLOWER = "follower0"
TWO_NET_PUBLIC = "public0"


def _init_two_network_devnet(request, base):
    """Init a two-network devnet: validators --WS--> follower0 --WS--> public0.

    The follower is dual-homed; the public node is on the public network only.
    Docker-only (needs the two bridge networks).  Returns the resolved ``data_dir``.
    """
    n = request.config.getoption("--consensus-validators")
    # n validator port-blocks + 2 more for the follower's and public node's
    # host-published RPC/WS ports (each service needs a full 6-port block).
    ports = find_free_base_ports(n + 2)
    val_ports, follow_port, public_port = ports[:n], ports[n], ports[n + 1]
    config = {
        "chain_id": 1337,
        "accounts": 200,
        "epoch_length": 100,
        "seed": 0,
        "tempo_bin": "tempo",  # in-image binary
        "tempo_xtask_bin": resolve_xtask_bin(),
        "validators": [{"host": "127.0.0.1", "port": p, "moniker": f"node{i}"} for i, p in enumerate(val_ports)],
        "docker": {
            "image": request.config.getoption("--tempo-image"),
            "validator_network": {"name": "tempo-2net-validators", "subnet": "10.90.0.0/24"},
            "public_network": {"name": "tempo-2net-public", "subnet": "10.91.0.0/24"},
            "follow_nodes": [{"moniker": TWO_NET_FOLLOWER, "port": follow_port}],
            "public_nodes": [{"moniker": TWO_NET_PUBLIC, "port": public_port}],
        },
    }
    return _run_devnet_init(base, config, gen_compose_file=True)


def _wait_public_node_synced(cluster, timeout: float = 150.0) -> None:
    """Wait until the public node has synced its first block from the follower.

    Advancing past genesis proves the whole chain works end to end. Validators
    bind RPC to their private IP (host-unreachable), so the public node's own
    published port is the observation point.
    """
    w3 = Web3(Web3.HTTPProvider(cluster.node_rpc_url(TWO_NET_PUBLIC)))
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        crashed = _crashed_nodes(cluster)
        if crashed:
            raise RuntimeError(f"containers crashed: {crashed}; see {cluster.data_dir}")
        try:
            if w3.eth.block_number >= 1:
                return
        except Exception as e:
            last_err = e
        time.sleep(1.0)
    raise TimeoutError(f"public node did not sync within {timeout}s (last error: {last_err})")


@pytest.fixture(scope="session")
def two_network_net(request, tmp_path_factory):
    """Two-network devnet with a follower and a public node (Docker only).

    The public node is on the public network only and syncs by WS-following the
    follower. Reach any node from the host via ``cluster.node_rpc_url(<moniker>)``.
    """
    if not request.config.getoption("--consensus-docker"):
        pytest.skip("two-network topology requires --consensus-docker")
    _skip_unless_docker_image(request)
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("two-network")
    data_dir = _init_two_network_devnet(request, base)
    yield from _serve_docker_cluster(request, base, data_dir, _wait_public_node_synced, label="two-network devnet")
