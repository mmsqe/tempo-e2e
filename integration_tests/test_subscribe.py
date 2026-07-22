"""WebSocket subscriptions (eth_subscribe)."""

import asyncio

import pytest
from tempo.constants import PATH_USD
from web3 import AsyncWeb3, WebSocketProvider

from .utils import TRANSFER_TOPIC, new_account, send_calls, transfer_call


@pytest.fixture
def ws_url(tempo) -> str:
    """The node's WebSocket URL; skip when attaching to an external node without one."""
    if not tempo.ws_url:
        pytest.skip("no WebSocket URL for the external node (set --tempo-ws / $TEMPO_WS)")
    return tempo.ws_url


async def _first_notification(ws, timeout=20):
    async def _recv():
        async for message in ws.socket.process_subscriptions():
            return message["result"]

    return await asyncio.wait_for(_recv(), timeout=timeout)


async def test_subscribe_new_heads(ws_url):
    async with AsyncWeb3(WebSocketProvider(ws_url)) as ws:
        await ws.eth.subscribe("newHeads")
        head = await _first_notification(ws)
    number = head["number"]
    assert (int(number, 16) if isinstance(number, str) else number) >= 1


async def test_subscribe_logs(w3, chain_id, ws_url, funded_account):
    async with AsyncWeb3(WebSocketProvider(ws_url)) as ws:
        await ws.eth.subscribe("logs", {"address": PATH_USD})
        # Trigger a transfer over HTTP; its Transfer log should arrive on the socket.
        await send_calls(
            w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 3)]
        )
        log = await _first_notification(ws)
    assert log["topics"][0] == TRANSFER_TOPIC
