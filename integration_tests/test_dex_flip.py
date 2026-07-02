"""TIP-1030 same-tick flip orders + TIP-1056 flip-in-place: a flip order, once
fully filled, re-rests on the opposite side under the SAME orderId (emitting
OrderFlipped, not a fresh OrderPlaced). Uses a freshly created token so the book
is empty and the maker's order is the only ask the taker's swap can fill.
"""

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo.constants import ALPHA_USD, PATH_USD
from tempo.constants import STABLECOIN_DEX_ADDRESS as DEX_ADDR

from .abi import DEX
from .utils import STATE_WRITE_GAS, call_revert, create_token, fund, fund_token, new_account, send_calls

pytestmark = pytest.mark.tempo

MAX_UINT = 2**256 - 1
ORDER_FLIPPED = keccak(text="OrderFlipped(uint128,address,address,uint128,bool,int16,int16)")
ORDER_PLACED = keccak(text="OrderPlaced(uint128,address,address,uint128,bool,int16,bool,int16)")


async def test_flip_order_flips_in_place_on_full_fill(w3, chain_id, funded_account):
    amount = 1_000_000_000  # >= MIN_ORDER_AMOUNT (1e8); tick 0 is 1:1 so the flip self-funds
    maker = new_account()
    await fund(w3, maker.address)
    token = await create_token(w3, chain_id=chain_id, admin=funded_account, mint=(maker.address, 20_000_000_000))

    order_id = await DEX.fns.nextOrderId().call(w3, to=DEX_ADDR)
    placed = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": ERC20.fns.approve(DEX_ADDR, MAX_UINT).data},
            {"to": DEX_ADDR, "data": DEX.fns.placeFlip(token, amount, False, 0, 0).data},  # ask, same-tick flip
        ],
    )
    assert placed["status"] == 1

    taker = new_account()
    await fund(w3, taker.address)
    await fund_token(w3, chain_id=chain_id, to=taker.address, token=PATH_USD, amount=5_000_000_000)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=taker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": PATH_USD, "data": ERC20.fns.approve(DEX_ADDR, MAX_UINT).data},
            {"to": DEX_ADDR, "data": DEX.fns.swapExactAmountIn(PATH_USD, token, amount, 0).data},  # full fill
        ],
    )
    assert receipt["status"] == 1

    logs = [lg for lg in receipt["logs"] if lg["address"].lower() == DEX_ADDR.lower()]
    flipped = [lg for lg in logs if HexBytes(lg["topics"][0]) == HexBytes(ORDER_FLIPPED)]
    assert len(flipped) == 1
    assert int.from_bytes(bytes(flipped[0]["topics"][1]), "big") == order_id  # same orderId
    assert not any(HexBytes(lg["topics"][0]) == HexBytes(ORDER_PLACED) for lg in logs)  # not re-placed

    order = await DEX.fns.getOrder(order_id).call(w3, to=DEX_ADDR)
    assert order[3]  # isBid flipped from ask to bid
    assert order[5] == amount and order[6] == amount  # amount kept, remaining reset to full
    assert order[9] and order[10] == 0  # isFlip stays true; flipTick == tick == 0


async def test_ask_fliptick_above_tick_reverts(w3, chain_id):
    maker = new_account()
    await fund(w3, maker.address)
    await fund_token(w3, chain_id=chain_id, to=maker.address, token=ALPHA_USD, amount=5_000_000_000)
    await send_calls(  # approve on-chain so escrow isn't the first failure in the eth_call below
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": ALPHA_USD, "data": ERC20.fns.approve(DEX_ADDR, MAX_UINT).data}],
    )
    # an ask requires flipTick <= tick; flipTick 10 > tick 0 -> InvalidFlipTick
    reason = await call_revert(
        w3, DEX_ADDR, DEX.fns.placeFlip(ALPHA_USD, 1_000_000_000, False, 0, 10).data, sender=maker.address
    )
    assert "InvalidFlipTick" in reason or "0xf88aeb80" in reason
