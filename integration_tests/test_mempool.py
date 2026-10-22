"""txpool & operator RPC namespaces."""

import pytest

from .utils import rpc_result

# allegro, a bare reth fork, serves neither the txpool namespace nor `operator_*`.
pytestmark = pytest.mark.requires("tempo-native")


async def test_txpool_status_has_pending_and_queued(w3):
    result = await rpc_result(w3, "txpool_status")
    assert "pending" in result and "queued" in result


async def test_txpool_content_has_pending_and_queued(w3):
    result = await rpc_result(w3, "txpool_content")
    assert "pending" in result and "queued" in result


async def test_operator_peers_returns_a_list(w3):
    assert isinstance(await rpc_result(w3, "operator_peers"), list)  # empty on a single dev node
