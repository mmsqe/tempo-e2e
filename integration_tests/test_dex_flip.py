"""TIP-1030 same-tick flip orders + TIP-1056 flip-in-place: once fully filled, a
flip order re-rests on the opposite side under the SAME orderId (emitting
OrderFlipped, not OrderPlaced). Tests use a fresh token so the book starts empty.
"""

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo.constants import ALPHA_USD, PATH_USD
from tempo.constants import STABLECOIN_DEX_ADDRESS as DEX_ADDR

from .abi import DEX
from .utils import (
    MAX_UINT,
    STATE_WRITE_GAS,
    call_revert,
    create_token,
    fund,
    fund_token,
    gas_cost_in_token,
    new_account,
    send_calls,
)

pytestmark = pytest.mark.tempo
AMOUNT = 1_000_000_000  # >= MIN_ORDER_AMOUNT (1e8); tick 0 is 1:1 so flips self-fund
ORDER_FLIPPED = keccak(text="OrderFlipped(uint128,address,address,uint128,bool,int16,int16)")
ORDER_PLACED = keccak(text="OrderPlaced(uint128,address,address,uint128,bool,int16,bool,int16)")


async def _approve_and(w3, chain_id, signer, token, call):
    """Approve ``token`` to the DEX and run one DEX call in the same batch."""
    return await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": ERC20.fns.approve(DEX_ADDR, MAX_UINT).data},
            {"to": DEX_ADDR, "data": call.data},
        ],
    )


async def _flipped_ask(w3, chain_id, admin):
    """A same-tick ask flip on a fresh token's book, fully filled so it now rests as a bid.

    Returns (maker, token, order_id, fill_receipt)."""
    maker = new_account()
    await fund(w3, maker.address)
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(maker.address, 20 * AMOUNT))
    order_id = await DEX.fns.nextOrderId().call(w3, to=DEX_ADDR)
    placed = await _approve_and(w3, chain_id, maker, token, DEX.fns.placeFlip(token, AMOUNT, False, 0, 0))
    assert placed["status"] == 1

    taker = new_account()
    await fund(w3, taker.address)
    await fund_token(w3, chain_id=chain_id, to=taker.address, token=PATH_USD, amount=5 * AMOUNT)
    fill = await _approve_and(w3, chain_id, taker, PATH_USD, DEX.fns.swapExactAmountIn(PATH_USD, token, AMOUNT, 0))
    assert fill["status"] == 1
    return maker, token, order_id, fill


async def test_flip_order_flips_in_place_on_full_fill(w3, chain_id, funded_account):
    _maker, _token, order_id, receipt = await _flipped_ask(w3, chain_id, funded_account)

    logs = [lg for lg in receipt["logs"] if lg["address"].lower() == DEX_ADDR.lower()]
    flipped = [lg for lg in logs if HexBytes(lg["topics"][0]) == HexBytes(ORDER_FLIPPED)]
    assert len(flipped) == 1
    assert int.from_bytes(bytes(flipped[0]["topics"][1]), "big") == order_id  # same orderId
    assert not any(HexBytes(lg["topics"][0]) == HexBytes(ORDER_PLACED) for lg in logs)  # not re-placed

    order = await DEX.fns.getOrder(order_id).call(w3, to=DEX_ADDR)
    assert order[3]  # isBid flipped from ask to bid
    assert order[5] == AMOUNT and order[6] == AMOUNT  # amount kept, remaining reset to full
    assert order[9] and order[10] == 0  # isFlip stays true; flipTick == tick == 0

    # TIP-1056: the flip re-rested under the SAME id -- only the initial place consumed an order id
    assert await DEX.fns.nextOrderId().call(w3, to=DEX_ADDR) == order_id + 1


async def test_cancel_after_flip_refunds_flipped_side(w3, chain_id, funded_account):
    """TIP-1056 motivating case: after an ask flips to a bid, cancel(orderId) targets the
    flipped order -- the maker gets the bid's QUOTE escrow back, not the original base."""
    maker, _token, order_id, _fill = await _flipped_ask(w3, chain_id, funded_account)

    path_before = await ERC20.fns.balanceOf(maker.address).call(w3, to=PATH_USD)
    assert await DEX.fns.balanceOf(maker.address, PATH_USD).call(w3, to=DEX_ADDR) == 0
    cancelled = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": DEX_ADDR, "data": DEX.fns.cancel(order_id).data},
            {"to": DEX_ADDR, "data": DEX.fns.withdraw(PATH_USD, AMOUNT).data},  # escrow -> wallet
        ],
    )
    assert cancelled["status"] == 1
    expected = path_before + AMOUNT - gas_cost_in_token(cancelled)  # quote refund, less gas paid in PATH
    assert await ERC20.fns.balanceOf(maker.address).call(w3, to=PATH_USD) == expected

    reason = await call_revert(w3, DEX_ADDR, DEX.fns.getOrder(order_id).data)
    assert "OrderDoesNotExist" in reason


async def test_bid_same_tick_flip_accepted_and_wrong_side_rejected(w3, chain_id, funded_account):
    """TIP-1030 bid side: flipTick == tick is legal at T5+, flipTick < tick never is."""
    maker = new_account()
    await fund(w3, maker.address)  # PATH-funded; a bid escrows the quote token
    token = await create_token(w3, chain_id=chain_id, admin=funded_account)

    order_id = await DEX.fns.nextOrderId().call(w3, to=DEX_ADDR)
    placed = await _approve_and(w3, chain_id, maker, PATH_USD, DEX.fns.placeFlip(token, AMOUNT, True, 0, 0))
    assert placed["status"] == 1
    order = await DEX.fns.getOrder(order_id).call(w3, to=DEX_ADDR)
    assert order[3] and order[9] and order[10] == 0  # a resting same-tick bid flip

    # a bid must flip to an equal-or-higher tick: flipTick -10 < tick 0 -> InvalidBidFlipTick
    reason = await call_revert(w3, DEX_ADDR, DEX.fns.placeFlip(token, AMOUNT, True, 0, -10).data, sender=maker.address)
    assert "InvalidBidFlipTick" in reason or "InvalidFlipTick" in reason


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
