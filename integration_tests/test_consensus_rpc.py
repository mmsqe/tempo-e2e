"""Consensus RPC namespace (consensus_*).

Needs the multi-validator consensus localnet; a single ``--dev`` node does not run
BFT consensus, so these skip when the namespace is absent.
"""

import pytest

pytestmark = pytest.mark.consensus


async def _consensus(w3, method, params=None):
    resp = await w3.provider.make_request(f"consensus_{method}", params or [])
    if (resp.get("error") or {}).get("code") == -32601:  # method not found
        pytest.skip("consensus_* RPC not exposed (needs the consensus localnet, not --dev)")
    return resp


async def test_get_latest_returns_state(w3):
    resp = await _consensus(w3, "getLatest")
    assert "error" not in resp, resp.get("error")
    assert "result" in resp


async def test_get_finalization_latest(w3):
    resp = await _consensus(w3, "getFinalization", ["latest"])
    # 204 (NoContent) is acceptable before the first finalization is archived.
    assert "result" in resp or (resp.get("error") or {}).get("code") == 204, resp
