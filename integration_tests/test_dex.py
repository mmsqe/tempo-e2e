"""Stablecoin DEX: limit orders and swaps against the PATH_USD quote."""

import pytest
from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo.constants import ALPHA_USD, PATH_USD, STABLECOIN_DEX_ADDRESS

from .abi import DEX
from .utils import MAX_UINT, STATE_WRITE_GAS, fund, fund_token, new_account, send_calls

pytestmark = pytest.mark.tempo


async def _trader(w3, chain_id, *, token=None, amount=0):
    """A fresh account with PATH (for gas) and, optionally, ``token``."""
    acct = new_account()
    await fund(w3, acct.address)
    if token:
        await fund_token(w3, chain_id=chain_id, to=acct.address, token=token, amount=amount)
    return acct


async def test_tick_price_round_trip(w3):
    price = await DEX.fns.tickToPrice(0).call(w3, to=STABLECOIN_DEX_ADDRESS)
    assert price > 0  # tick 0 == 1:1
    assert await DEX.fns.priceToTick(price).call(w3, to=STABLECOIN_DEX_ADDRESS) == 0
    assert await DEX.fns.MIN_ORDER_AMOUNT().call(w3, to=STABLECOIN_DEX_ADDRESS) > 0


async def test_place_get_and_cancel_order(w3, chain_id):
    maker = await _trader(w3, chain_id, token=ALPHA_USD, amount=10_000_000_000)
    order_id = await DEX.fns.nextOrderId().call(w3, to=STABLECOIN_DEX_ADDRESS)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": ALPHA_USD, "data": ERC20.fns.approve(STABLECOIN_DEX_ADDRESS, MAX_UINT).data},
            {"to": STABLECOIN_DEX_ADDRESS, "data": DEX.fns.place(ALPHA_USD, 2_000_000_000, False, 0).data},
        ],
    )
    assert receipt["status"] == 1

    order = await DEX.fns.getOrder(order_id).call(w3, to=STABLECOIN_DEX_ADDRESS)
    assert HexBytes(order[1]) == HexBytes(maker.address)  # maker
    assert order[5] == 2_000_000_000  # amount

    cancel = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": STABLECOIN_DEX_ADDRESS, "data": DEX.fns.cancel(order_id).data}],
    )
    assert cancel["status"] == 1
    with pytest.raises(Exception):  # OrderDoesNotExist
        await DEX.fns.getOrder(order_id).call(w3, to=STABLECOIN_DEX_ADDRESS)


async def test_swap_fills_resting_order(w3, chain_id):
    # Maker rests an ask on ALPHA (sell ALPHA for PATH) at 1:1.
    maker = await _trader(w3, chain_id, token=ALPHA_USD, amount=20_000_000_000)
    await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": ALPHA_USD, "data": ERC20.fns.approve(STABLECOIN_DEX_ADDRESS, MAX_UINT).data},
            {"to": STABLECOIN_DEX_ADDRESS, "data": DEX.fns.place(ALPHA_USD, 5_000_000_000, False, 0).data},
        ],
    )

    # Taker swaps PATH -> ALPHA against the resting ask.
    taker = await _trader(w3, chain_id, token=PATH_USD, amount=5_000_000_000)
    amount_in = 1_000_000_000
    quote = await DEX.fns.quoteSwapExactAmountIn(PATH_USD, ALPHA_USD, amount_in).call(w3, to=STABLECOIN_DEX_ADDRESS)
    before = await ERC20.fns.balanceOf(taker.address).call(w3, to=ALPHA_USD)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=taker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": PATH_USD, "data": ERC20.fns.approve(STABLECOIN_DEX_ADDRESS, MAX_UINT).data},
            {"to": STABLECOIN_DEX_ADDRESS, "data": DEX.fns.swapExactAmountIn(PATH_USD, ALPHA_USD, amount_in, 0).data},
        ],
    )
    assert receipt["status"] == 1
    after = await ERC20.fns.balanceOf(taker.address).call(w3, to=ALPHA_USD)
    assert after - before == quote > 0
