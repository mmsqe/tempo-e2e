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
from web3 import Web3

from . import tidx as tidx_mod
from .docker_cluster import DockerCluster
from .drivers import get_driver
from .drivers.base import CAP_CONSENSUS_NET, CAP_INDEXER, CAP_TEMPO_NATIVE
from .network import ExternalNode, free_port, resolve_tempo_bin, resolve_xtask_bin
from .utils import new_account, rpc

if not os.environ.get("TMPDIR", "").startswith("/tmp"):
    os.environ["TMPDIR"] = "/tmp"
    tempfile.tempdir = "/tmp"


# Legacy feature marks map onto capability tokens so existing tests gate cleanly.
_MARK_CAPABILITY = {"tempo": CAP_TEMPO_NATIVE, "consensus": CAP_CONSENSUS_NET}


def pytest_configure(config):
    config.addinivalue_line("markers", "requires(cap): skip unless the active --backend advertises capability `cap`")
    # tidx indexes the node from a container, so the node has to listen off-loopback.
    # Set before the `tempo` fixture builds the command line.
    if config.getoption("--tidx"):
        os.environ["TEMPO_HTTP_ADDR"] = "0.0.0.0"
    if config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = config.getoption("--tempo-bin")


def pytest_collection_modifyitems(config, items):
    """Skip tests whose required capabilities the active backend lacks."""
    caps = get_driver(config.getoption("--backend")).capabilities()
    # Orthogonal to --tidx: a second index alongside the node does not make the node's
    # own handlers answer, so the differential tests need both flags.
    if config.getoption("--indexer"):
        caps |= {CAP_INDEXER}
    for item in items:
        required = {cap for mark, cap in _MARK_CAPABILITY.items() if item.get_closest_marker(mark)}
        required |= {arg for mark in item.iter_markers("requires") for arg in mark.args}
        missing = required - caps
        if missing:
            item.add_marker(
                pytest.mark.skip(reason=f"backend {config.getoption('--backend')!r} lacks {sorted(missing)}")
            )


@pytest.fixture(scope="session")
def driver(request):
    """The active backend driver (``--backend`` / ``$E2E_BACKEND``, default tempo)."""
    return get_driver(request.config.getoption("--backend"))


@pytest.fixture(scope="session")
def tempo(request, driver, tmp_path_factory):
    """The session's node: a fresh dev node from the backend, or the running one ``--tempo-rpc`` names."""
    rpc_url = request.config.getoption("--tempo-rpc")
    if rpc_url:
        yield ExternalNode(rpc_url, request.config.getoption("--tempo-ws")).wait_for_rpc()
        return

    base = tmp_path_factory.mktemp(driver.name)
    node = driver.dev_node(base, log_name=f"{driver.name}.log")
    try:
        node.start().wait_for_rpc()
        yield node
    finally:
        node.stop()
        if request.config.getoption("--clean-data"):
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def w3(tempo):
    async with rpc(tempo) as client:
        yield client


@pytest.fixture
def chain_id(tempo) -> int:
    return tempo.chain_id


@pytest.fixture(scope="session")
def tidx(request, driver, tempo, tmp_path_factory):
    """tempoxyz/tidx indexing the session node (``--tidx``); skips without docker or its image."""
    if not request.config.getoption("--tidx"):
        pytest.skip("tidx oracle not requested (pass --tidx)")
    # tidx decodes tempo's block shape, not plain Ethereum's: it requires the
    # `timestampMillis` header field tempo adds, and its sync loop errors on every
    # batch without it. Checked here rather than left to the readiness timeout, which
    # would otherwise burn three minutes and report a generic "did not come up".
    if CAP_TEMPO_NATIVE not in driver.capabilities():
        pytest.skip(f"tidx only decodes tempo-format blocks; backend {driver.name!r} serves plain Ethereum ones")
    try:
        tidx_mod.preflight()
    except tidx_mod.TidxUnavailable as e:
        pytest.skip(str(e))

    base = tmp_path_factory.mktemp("tidx")
    stack = tidx_mod.Tidx(
        base,
        chain_id=tempo.chain_id,
        rpc_url=tidx_mod.rpc_url_for_container(tempo.rpc_url),
        api_port=free_port(),
    )
    try:
        try:
            stack.up().wait_for_block(0)  # the first block landing proves the whole path works
        except tidx_mod.TidxUnavailable as e:
            pytest.skip(f"tidx did not come up: {e}")
        yield stack
    finally:
        stack.down()
        if request.config.getoption("--clean-data"):
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def account():
    return new_account()


@pytest.fixture
async def funded_account(w3, driver):
    acct = new_account()
    await driver.fund(w3, acct.address, 10_000_000_000_000_000)
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


def _wait_until(cluster, ready, what: str, timeout: float) -> None:
    """Poll ``ready()`` until it holds, failing at once if a node crashes."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        if crashed := _crashed_nodes(cluster):
            raise RuntimeError(f"nodes crashed: {crashed}; see {cluster.data_dir}")
        try:
            if ready():
                return
        except Exception as e:  # noqa: BLE001 - still coming up
            last_err = e
        time.sleep(1.0)
    raise TimeoutError(f"{what} within {timeout}s (last error: {last_err})")


def _wait_for_finalization(cluster, timeout: float = 120.0) -> None:
    """Wait until node0 finalizes a block through consensus."""
    w3 = Web3(Web3.HTTPProvider(cluster.node_rpc_url("node0")))

    def finalized():
        latest = (w3.provider.make_request("consensus_getLatest", []).get("result") or {}).get("finalized")
        return latest and latest.get("view", 0) >= 1 and w3.eth.block_number >= 1

    _wait_until(cluster, finalized, "consensus did not finalize", timeout)


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


def _devnet_config(ports: list[int], *, tempo_bin: str, **extra) -> dict:
    """A ``tempo-devnet`` config with a validator per free base port; xtask always runs on the host."""
    return {
        "chain_id": 1337,
        "seed": 0,
        "tempo_bin": tempo_bin,
        "tempo_xtask_bin": resolve_xtask_bin(),
        "validators": [{"host": "127.0.0.1", "port": port, "moniker": f"node{i}"} for i, port in enumerate(ports)],
        **extra,
    }


@pytest.fixture(scope="session")
def consensus_net(request, tmp_path_factory):
    """A localnet under supervisord (``--consensus``) or docker compose (``--consensus-docker``); both
    handles expose the same node controls."""
    docker = request.config.getoption("--consensus-docker")
    if not (docker or request.config.getoption("--consensus")):
        pytest.skip("consensus localnet not requested (pass --consensus or --consensus-docker)")
    if docker:
        _skip_unless_docker_image(request)

    base = tmp_path_factory.mktemp("consensus-docker" if docker else "consensus")
    ports = find_free_base_ports(request.config.getoption("--consensus-validators"))
    # Docker runs the in-image `tempo`; supervisord runs the host binary.
    config = _devnet_config(ports, tempo_bin="tempo" if docker else resolve_tempo_bin(), accounts=200, epoch_length=100)
    if docker:
        config["docker"] = {"image": request.config.getoption("--tempo-image"), "network": "tempo-devnet"}
    data_dir = _run_devnet_init(base, config, gen_compose_file=docker)

    if docker:
        yield from _serve_docker_cluster(
            request, base, data_dir, _wait_for_finalization, label="docker consensus localnet"
        )
    else:
        yield from _consensus_net_supervisord(request, base, data_dir)


def supervisord_net(request, base, validators: int, *, accounts: int = 20, **extra):
    """A module's own ``validators``-node localnet under supervisord; skips without ``--consensus``."""
    if not request.config.getoption("--consensus"):
        pytest.skip("consensus localnet not requested (pass --consensus)")
    ports = find_free_base_ports(validators)
    config = _devnet_config(ports, tempo_bin=resolve_tempo_bin(), accounts=accounts, **extra)
    yield from _consensus_net_supervisord(request, base, _run_devnet_init(base, config, gen_compose_file=False))


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


def _skip_unless_docker_image(request) -> None:
    if shutil.which("docker") is None:
        pytest.skip("--consensus-docker requested but the docker CLI is not available")
    image = request.config.getoption("--tempo-image")
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        pytest.skip(
            f"--consensus-docker: image {image!r} not found locally. Build/pull it first "
            f"(e.g. `docker buildx bake tempo --load`), or point --tempo-image/$TEMPO_IMAGE at an existing image."
        )


def _serve_docker_cluster(request, base, data_dir, wait_ready, *, label):
    """Bring up ``data_dir``'s compose stack and yield it once ready, with compose and container logs
    on failure."""
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


@pytest.fixture(scope="session")
def num_validators(request) -> int:
    """Validator count of the consensus localnet (see ``--consensus-validators``)."""
    return request.config.getoption("--consensus-validators")


@pytest.fixture
async def consensus_w3(consensus_net):
    async with rpc(consensus_net.node_rpc_url("node0")) as client:
        yield client


TWO_NET_FOLLOWER = "follower0"
TWO_NET_PROXY = "proxy0"
TWO_NET_PUBLIC = "public0"


def _init_two_network_devnet(request, base):
    """Init the docker-only two-network devnet: validators, a follower, a P2P proxy and a public node."""
    n = request.config.getoption("--consensus-validators")
    # n validator port-blocks + 3 more for the follower, proxy, and public node
    # host-published ports (each service needs a full 6-port block).
    ports = find_free_base_ports(n + 3)
    val_ports, follow_port, proxy_port, public_port = ports[:n], ports[n], ports[n + 1], ports[n + 2]
    config = _devnet_config(
        val_ports,
        tempo_bin="tempo",  # in-image binary
        accounts=200,
        epoch_length=100,
        docker={
            "image": request.config.getoption("--tempo-image"),
            "validator_network": {"name": "tempo-2net-validators", "subnet": "10.90.0.0/24"},
            "public_network": {"name": "tempo-2net-public", "subnet": "10.91.0.0/24"},
            "follow_nodes": [{"moniker": TWO_NET_FOLLOWER, "port": follow_port}],
            "p2p_proxies": [{"moniker": TWO_NET_PROXY, "port": proxy_port}],
            "public_nodes": [{"moniker": TWO_NET_PUBLIC, "port": public_port}],
        },
    )
    return _run_devnet_init(base, config, gen_compose_file=True)


def _wait_public_node_synced(cluster, timeout: float = 150.0) -> None:
    """Wait until the public node syncs block 1 from the follower; validators' RPC is not reachable
    from the host."""
    w3 = Web3(Web3.HTTPProvider(cluster.node_rpc_url(TWO_NET_PUBLIC)))
    _wait_until(cluster, lambda: w3.eth.block_number >= 1, "public node did not sync", timeout)


@pytest.fixture(scope="session")
def two_network_net(request, tmp_path_factory):
    """The two-network devnet (``--consensus-docker``); reach a node with ``cluster.node_rpc_url(moniker)``."""
    if not request.config.getoption("--consensus-docker"):
        pytest.skip("two-network topology requires --consensus-docker")
    _skip_unless_docker_image(request)

    base = tmp_path_factory.mktemp("two-network")
    data_dir = _init_two_network_devnet(request, base)
    yield from _serve_docker_cluster(request, base, data_dir, _wait_public_node_synced, label="two-network devnet")
