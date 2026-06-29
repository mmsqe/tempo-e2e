"""Pytest fixtures and options for the tempo e2e suite."""

from __future__ import annotations

import os
import shutil
import time

import pytest
from web3 import AsyncWeb3

from .consensus_net import ConsensusNetwork
from .network import TempoNode, free_port
from .utils import fund, new_account


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


@pytest.fixture(scope="session")
def consensus_net(request, tmp_path_factory):
    """A 4-validator consensus localnet (opt-in via --consensus)."""
    if not request.config.getoption("--consensus"):
        pytest.skip("consensus localnet not requested (pass --consensus)")
    if request.config.getoption("--tempo-bin"):
        os.environ["TEMPO_BIN"] = request.config.getoption("--tempo-bin")

    base = tmp_path_factory.mktemp("consensus")
    net = ConsensusNetwork(base_dir=base)
    net.generate()
    try:
        last_err: Exception | None = None
        for _ in range(5):  # retry: a freshly-picked port can be grabbed before launch
            try:
                net.start().wait_for_finalization()
                break
            except (RuntimeError, TimeoutError) as e:
                last_err = e
                net.stop()
                time.sleep(5)
        else:
            raise last_err
        yield net
    finally:
        net.stop()
        if not request.config.getoption("--keep-data"):
            shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
async def consensus_w3(consensus_net):
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(consensus_net.rpc_url))
    yield client
    await client.provider.disconnect()
