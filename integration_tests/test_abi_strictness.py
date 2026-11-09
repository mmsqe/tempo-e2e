"""TIP-1100 precompile calldata: strict decoding from T11, trailing bytes allowed again at T12.

Every precompile shares the decoder, so one TIP-20 getter stands in for all of them."""

from __future__ import annotations

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import active_forks, call_revert, rpc, skip_before

pytestmark = pytest.mark.tempo

# TIP-1100 prices a word of calldata at 6 gas before T11 and 30 from it.
T11_WORD_RISE = 24

HOLDER = "0x" + "11" * 20
BALANCE_OF = bytes(ERC20.fns.balanceOf(HOLDER).data)
WORDS = -(-len(BALANCE_OF) // 32)


def payload(data: bytes) -> dict:
    return {"to": PATH_USD, "data": "0x" + data.hex()}


async def test_trailing_bytes_are_accepted_from_t12(w3):
    """A suffix past the last argument decodes again at T12; T11 alone refuses it."""
    forks = await active_forks(w3)
    if "T11" in forks and "T12" not in forks:
        for suffix in (1, 32, 33):
            assert await call_revert(w3, PATH_USD, BALANCE_OF + b"\xff" * suffix) == "execution reverted"
        return
    # `call_revert` asserts a refusal, so it cannot ask whether there is one.
    trailing = await w3.provider.make_request("eth_call", [payload(BALANCE_OF + b"\xff"), "latest"])
    if "T12" in forks and "error" in trailing:
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


@pytest.mark.slow
async def test_t11_charges_more_per_input_word(w3, held_back):
    """Each word of precompile calldata costs 24 gas more once T11 is active."""
    await skip_before(w3, "T11")

    async with rpc(held_back("t11_time")) as before:
        assert "T11" not in await active_forks(before)
        rise = await w3.eth.estimate_gas(payload(BALANCE_OF)) - await before.eth.estimate_gas(payload(BALANCE_OF))

    # estimate_gas searches for the limit, so it can land a gas or two above the true cost.
    assert WORDS * T11_WORD_RISE <= rise < WORDS * (T11_WORD_RISE + 1)
