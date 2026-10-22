"""Liveness: the node is up and producing blocks."""

from .utils import wait_for_block


async def test_node_is_connected(w3):
    assert await w3.is_connected()


async def test_chain_has_started(w3, chain_id):
    assert await w3.eth.block_number >= 1
    assert chain_id > 0


async def test_blocks_advance(w3):
    start = await w3.eth.block_number
    await wait_for_block(w3, start + 1, timeout=15)
    assert await w3.eth.block_number > start
