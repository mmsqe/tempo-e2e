"""Liveness: the node is up and producing blocks."""

import asyncio
import time


async def test_node_is_connected(w3):
    assert await w3.is_connected()


async def test_chain_has_started(w3, chain_id):
    assert await w3.eth.block_number >= 1
    assert chain_id > 0


async def test_blocks_advance(w3):
    start = await w3.eth.block_number
    deadline = time.time() + 15
    while await w3.eth.block_number <= start and time.time() < deadline:
        await asyncio.sleep(0.5)
    assert await w3.eth.block_number > start
