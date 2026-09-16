"""TIP-1100 precompile calldata: strict decoding from T11, trailing bytes allowed again at T12.

Strict decoding refuses what the lenient decoder waved through, and it refuses before the
precompile runs, so the revert carries no error selector. Every precompile shares the decoder,
so one TIP-20 getter stands in for all of them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import AsyncWeb3

from .network import dev_node, xtask_forks
from .utils import active_forks, call_revert

pytestmark = pytest.mark.tempo

# A timestamp no run reaches, so a fork scheduled at it never activates.
FAR_FUTURE = 4_000_000_000

# TIP-1100 prices a word of calldata at 6 gas before T11 and 30 from it.
T11_WORD_RISE = 24

HOLDER = "0x" + "11" * 20
BALANCE_OF = bytes(ERC20.fns.balanceOf(HOLDER).data)
WORDS = -(-len(BALANCE_OF) // 32)


def payload(data: bytes) -> dict:
    return {"to": PATH_USD, "data": "0x" + data.hex()}


async def refuses(w3, data: bytes) -> bool:
    """Whether the node rejects ``data``. `call_revert` asserts one, so it cannot ask."""
    resp = await w3.provider.make_request("eth_call", [payload(data), "latest"])
    return resp.get("error") is not None


async def test_trailing_bytes_are_accepted_from_t12(w3):
    """A suffix past the last argument decodes again at T12; T11 alone refuses it.

    The relaxation landed after v1.14.0, so a node from that line schedules T12 and refuses.
    """
    forks = await active_forks(w3)
    if "T11" in forks and "T12" not in forks:
        for suffix in (1, 32, 33):
            assert await call_revert(w3, PATH_USD, BALANCE_OF + b"\xff" * suffix) == "execution reverted"
        return
    if "T12" in forks and await refuses(w3, BALANCE_OF + b"\xff"):
        pytest.skip("the node schedules T12 but its decoder refuses trailing bytes")
    for suffix in (1, 32, 33):
        data = BALANCE_OF + b"\xff" * suffix
        assert await w3.eth.call(payload(data)) == await w3.eth.call(payload(BALANCE_OF))


async def test_dirty_address_padding_is_refused_at_t11(w3):
    """The twelve bytes above an address must be zero once the decoder is strict."""
    dirty = BALANCE_OF[:4] + b"\xff" * 12 + bytes.fromhex(HOLDER[2:])
    if "T11" in await active_forks(w3):
        # Decoding fails before dispatch, so there is no error selector to report.
        assert await call_revert(w3, PATH_USD, dirty) == "execution reverted"
    else:
        assert await w3.eth.call(payload(dirty)) == await w3.eth.call(payload(BALANCE_OF))


@pytest.fixture(scope="module")
def pre_t11_node():
    """A node held at the fork before T11, to price the same call on both sides."""
    order = xtask_forks()
    if "t11_time" not in order:
        pytest.skip("tempo-xtask cannot schedule t11_time")
    held_back = {fork: FAR_FUTURE for fork in order[order.index("t11_time") :]}
    node = dev_node(Path(tempfile.mkdtemp()), log_name="pre_t11.log", fork_times=held_back)
    try:
        node.start().wait_for_rpc()
        yield node
    finally:
        node.stop()


@pytest.mark.slow
async def test_t11_charges_more_per_input_word(w3, pre_t11_node):
    """Each word of precompile calldata costs 24 gas more once T11 is active."""
    if "T11" not in await active_forks(w3):
        pytest.skip("the node under test is older than T11")

    before = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(pre_t11_node.rpc_url))
    try:
        assert "T11" not in await active_forks(before)
        rise = await w3.eth.estimate_gas(payload(BALANCE_OF)) - await before.eth.estimate_gas(payload(BALANCE_OF))
    finally:
        await before.provider.disconnect()

    # estimate_gas searches for the limit, so it can land a gas or two above the true cost.
    assert WORDS * T11_WORD_RISE <= rise < WORDS * (T11_WORD_RISE + 1)
