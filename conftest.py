import os


def pytest_addoption(parser):
    group = parser.getgroup("tempo")
    group.addoption(
        "--backend",
        default=os.environ.get("E2E_BACKEND", "tempo"),
        help="Node backend driver to test against (tempo | allegro | …; also $E2E_BACKEND)",
    )
    group.addoption("--tempo-bin", default=None, help="Path to the tempo node binary")
    group.addoption(
        "--tempo-rpc",
        default=os.environ.get("TEMPO_RPC"),
        help="Attach to an already-running node at this RPC URL instead of launching one, "
        "and leave it running (also $TEMPO_RPC).",
    )
    group.addoption(
        "--tempo-ws",
        default=os.environ.get("TEMPO_WS"),
        help="WebSocket URL of the --tempo-rpc node (also $TEMPO_WS); only the "
        "eth_subscribe tests need it, and they skip when it is unset.",
    )
    group.addoption(
        "--clean-data",
        action="store_true",
        default=False,
        help="Remove the node datadir after the run (kept by default for inspection)",
    )
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
    group.addoption(
        "--consensus-validators",
        type=int,
        default=4,
        help="Number of validators in the consensus localnet (default 4 tolerates 1 fault)",
    )
    group.addoption(
        "--indexer",
        action="store_true",
        default=False,
        help="A live indexer backs the indexer RPCs; run the semantic tests (they skip by default, "
        "since tempo only registers stub handlers)",
    )
    group.addoption(
        "--tidx",
        action="store_true",
        default=False,
        help="Run tempoxyz/tidx (docker: tidx + postgres) as a differential oracle, and bind the "
        "node's RPC to 0.0.0.0 so the container can reach it. Combine with --indexer: tidx indexes "
        "the chain independently, it does not make the node's own handlers answer",
    )
