"""Consensus RPC namespace (consensus_*) against the multi-validator localnet (--consensus)."""

import asyncio

import pytest

pytestmark = pytest.mark.consensus


async def test_get_latest_returns_finalized_state(consensus_w3):
    resp = await consensus_w3.provider.make_request("consensus_getLatest", [])
    assert "error" not in resp, resp.get("error")
    finalized = resp["result"]["finalized"]
    assert finalized["view"] >= 1
    assert finalized["digest"].startswith("0x")


async def test_get_finalization_latest_certifies_a_block(consensus_w3):
    # The marshal archive lags finalization slightly; poll until a certificate lands.
    resp = None
    for _ in range(15):
        resp = await consensus_w3.provider.make_request("consensus_getFinalization", ["latest"])
        if "result" in resp:
            assert int(resp["result"]["block"]["header"]["number"], 16) >= 1
            return
        await asyncio.sleep(1)
    pytest.fail(f"no finalization certificate from consensus_getFinalization: {resp}")


async def test_blocks_are_produced(consensus_w3):
    assert await consensus_w3.eth.block_number >= 1
