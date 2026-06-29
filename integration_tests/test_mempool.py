"""txpool RPC namespace."""


async def test_txpool_status_has_pending_and_queued(w3):
    resp = await w3.provider.make_request("txpool_status", [])
    assert "error" not in resp, resp.get("error")
    assert "pending" in resp["result"] and "queued" in resp["result"]


async def test_txpool_content_has_pending_and_queued(w3):
    resp = await w3.provider.make_request("txpool_content", [])
    assert "error" not in resp, resp.get("error")
    assert "pending" in resp["result"] and "queued" in resp["result"]
