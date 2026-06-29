"""Consensus RPC namespace (consensus_*) against the multi-validator localnet (--consensus)."""

import asyncio
import time

import pytest
from web3 import Web3

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


def _height(rpc_url: str) -> int:
    try:
        return Web3(Web3.HTTPProvider(rpc_url)).eth.block_number
    except Exception:  # noqa: BLE001 - node may be momentarily unreachable
        return -1


def _wait_height(rpc_url: str, target: int, timeout: float = 60.0) -> int:
    deadline = time.time() + timeout
    while _height(rpc_url) < target and time.time() < deadline:
        time.sleep(1.0)
    return _height(rpc_url)


# Runs last (after the RPC tests) and restores the network, since it disrupts the shared localnet.
def test_chain_survives_validator_restart(consensus_net):
    """Chain keeps finalizing with one validator down (3/4 quorum), and it rejoins (P0-02)."""
    primary = consensus_net.rpc_url  # validator 0 stays up throughout
    start = _height(primary)

    consensus_net.stop_one(1)
    assert _wait_height(primary, start + 3) >= start + 3, "chain halted with one validator down"
    progressed = _height(primary)

    consensus_net.start_one(1)
    rejoined = f"http://127.0.0.1:{consensus_net.http_ports[1]}"
    assert _wait_height(rejoined, progressed) >= progressed, "restarted validator did not catch up"
