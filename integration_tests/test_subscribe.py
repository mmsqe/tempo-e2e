"""WebSocket subscriptions (eth_subscribe)."""

import asyncio

from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo.constants import PATH_USD
from web3 import AsyncWeb3, WebSocketProvider

from .utils import new_account, send_calls

TRANSFER_TOPIC = HexBytes("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")


async def _first_notification(ws, timeout=20):
    async def _recv():
        async for message in ws.socket.process_subscriptions():
            return message["result"]

    return await asyncio.wait_for(_recv(), timeout=timeout)


async def test_subscribe_new_heads(tempo):
    async with AsyncWeb3(WebSocketProvider(tempo.ws_url)) as ws:
        await ws.eth.subscribe("newHeads")
        head = await _first_notification(ws)
    number = head["number"]
    assert (int(number, 16) if isinstance(number, str) else number) >= 1


async def test_subscribe_logs(w3, chain_id, tempo, funded_account):
    async with AsyncWeb3(WebSocketProvider(tempo.ws_url)) as ws:
        await ws.eth.subscribe("logs", {"address": PATH_USD})
        # Trigger a transfer over HTTP; its Transfer log should arrive on the socket.
        await send_calls(
            w3,
            chain_id=chain_id,
            private_key=funded_account.key.hex(),
            calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 3).data}],
        )
        log = await _first_notification(ws)
    assert log["topics"][0] == TRANSFER_TOPIC
