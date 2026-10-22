"""Stablecoin DEX precompile: ticks, limit orders, swaps, TIP-1030/1056 flip orders, the TIP-1087 book
index. Most tests use a fresh TIP-20, so its PATH_USD book starts empty and swaps fill only their own orders."""

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo.constants import ALPHA_USD, PATH_USD
from tempo.constants import STABLECOIN_DEX_ADDRESS as DEX_ADDR

from .abi import DEX
from .utils import (
    STATE_WRITE_GAS,
    approve_call,
    call_revert,
    create_token,
    fund_token,
    funded,
    gas_cost_in_token,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo

AMOUNT = 1_000_000_000  # an order / wall; >= MIN_ORDER_AMOUNT (1e8), tick 0 is 1:1 so flips self-fund
SWAP = 100_000_000  # per-swap amount

ORDER_FLIPPED = keccak(text="OrderFlipped(uint128,address,address,uint128,bool,int16,int16)")
ORDER_PLACED = keccak(text="OrderPlaced(uint128,address,address,uint128,bool,int16,bool,int16)")

# The DEX starts with no books, so the key vector never grows an index this high.
UNUSED_INDEX = 2**32 - 1


async def _trader(w3, chain_id, *, token=None, amount=0):
    """A fresh PATH-funded account (PATH pays gas), optionally holding ``amount`` of ``token``."""
    acct = await funded(w3)
    if token:
        await fund_token(w3, chain_id=chain_id, to=acct.address, token=token, amount=amount)
    return acct


async def _maker_with_token(w3, chain_id, admin, *, mint=AMOUNT):
    """A fresh PATH-funded account holding ``mint`` of a brand-new TIP-20 (empty book)."""
    maker = await funded(w3)
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(maker.address, mint))
    return maker, token


async def _approve_and(w3, chain_id, signer, tokens, *dex_calls):
    """Approve ``tokens`` (one address or several) to the DEX, then run ``dex_calls`` in one batch."""
    if isinstance(tokens, str):
        tokens = [tokens]
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            *(approve_call(DEX_ADDR, t) for t in tokens),
            *({"to": DEX_ADDR, "data": c.data} for c in dex_calls),
        ],
    )
    assert receipt["status"] == 1
    return receipt


# ---- ticks & prices ----


async def test_tick_price_round_trip(w3):
    price = await DEX.fns.tickToPrice(0).call(w3)
    assert price > 0  # tick 0 == 1:1
    assert await DEX.fns.priceToTick(price).call(w3) == 0
    assert await DEX.fns.MIN_ORDER_AMOUNT().call(w3) > 0


# ---- limit orders: place / get / cancel ----


async def test_place_get_and_cancel_order(w3, chain_id):
    maker = await _trader(w3, chain_id, token=ALPHA_USD, amount=10_000_000_000)
    order_id = await DEX.fns.nextOrderId().call(w3)
    await _approve_and(w3, chain_id, maker, ALPHA_USD, DEX.fns.place(ALPHA_USD, 2_000_000_000, False, 0))

    order = await DEX.fns.getOrder(order_id).call(w3)
    assert HexBytes(order[1]) == HexBytes(maker.address)  # maker
    assert order[5] == 2_000_000_000  # amount

    await send_call(w3, chain_id, maker, DEX_ADDR, DEX.fns.cancel(order_id).data)
    assert "OrderDoesNotExist" in await call_revert(w3, DEX_ADDR, DEX.fns.getOrder(order_id).data)


# ---- swaps ----


async def test_swap_fills_resting_order(w3, chain_id):
    # Maker rests an ask on ALPHA (sell ALPHA for PATH) at 1:1.
    maker = await _trader(w3, chain_id, token=ALPHA_USD, amount=20_000_000_000)
    await _approve_and(w3, chain_id, maker, ALPHA_USD, DEX.fns.place(ALPHA_USD, 5_000_000_000, False, 0))

    # Taker swaps PATH -> ALPHA against the resting ask.
    taker = await _trader(w3, chain_id, token=PATH_USD, amount=5_000_000_000)
    amount_in = 1_000_000_000
    quote = await DEX.fns.quoteSwapExactAmountIn(PATH_USD, ALPHA_USD, amount_in).call(w3)
    before = await ERC20.fns.balanceOf(taker.address).call(w3, to=ALPHA_USD)
    await _approve_and(w3, chain_id, taker, PATH_USD, DEX.fns.swapExactAmountIn(PATH_USD, ALPHA_USD, amount_in, 0))
    after = await ERC20.fns.balanceOf(taker.address).call(w3, to=ALPHA_USD)
    assert after - before == quote > 0


async def test_swap_round_trip_against_self_placed_walls(w3, chain_id, funded_account):
    """Seed a bid and an ask wall on a fresh pair, then swap both ways against the account's own book."""
    trader, token = await _maker_with_token(w3, chain_id, funded_account, mint=100 * AMOUNT)
    await fund_token(w3, chain_id=chain_id, to=trader.address, token=PATH_USD, amount=100 * AMOUNT)

    first_order = await DEX.fns.nextOrderId().call(w3)
    await _approve_and(  # approve both sides, then seed a bid wall and an ask wall
        w3,
        chain_id,
        trader,
        [token, PATH_USD],
        DEX.fns.placeFlip(token, AMOUNT, True, -100, 100),
        DEX.fns.placeFlip(token, AMOUNT, False, 100, -100),
    )
    assert await DEX.fns.nextOrderId().call(w3) == first_order + 2  # both walls rest

    # Filled against the bid wall, then the ask wall.
    await send_call(w3, chain_id, trader, DEX_ADDR, DEX.fns.swapExactAmountIn(token, PATH_USD, SWAP, 0).data)
    await send_call(w3, chain_id, trader, DEX_ADDR, DEX.fns.swapExactAmountIn(PATH_USD, token, SWAP, 0).data)


# ---- TIP-1030 / TIP-1056 same-tick flip orders ----
# A fully filled flip order re-rests on the other side under the same orderId (OrderFlipped, not OrderPlaced).


async def _flipped_ask(w3, chain_id, admin):
    """A same-tick ask flip on a fresh book, filled so it rests as a bid: (maker, token, order_id, fill)."""
    maker, token = await _maker_with_token(w3, chain_id, admin, mint=20 * AMOUNT)
    order_id = await DEX.fns.nextOrderId().call(w3)
    await _approve_and(w3, chain_id, maker, token, DEX.fns.placeFlip(token, AMOUNT, False, 0, 0))

    taker = await _trader(w3, chain_id, token=PATH_USD, amount=5 * AMOUNT)
    fill = await _approve_and(w3, chain_id, taker, PATH_USD, DEX.fns.swapExactAmountIn(PATH_USD, token, AMOUNT, 0))
    return maker, token, order_id, fill


async def test_flip_order_flips_in_place_on_full_fill(w3, chain_id, funded_account):
    _maker, _token, order_id, receipt = await _flipped_ask(w3, chain_id, funded_account)

    logs = [lg for lg in receipt["logs"] if lg["address"].lower() == DEX_ADDR.lower()]
    flipped = [lg for lg in logs if HexBytes(lg["topics"][0]) == HexBytes(ORDER_FLIPPED)]
    assert len(flipped) == 1
    assert int.from_bytes(bytes(flipped[0]["topics"][1]), "big") == order_id  # same orderId
    assert not any(HexBytes(lg["topics"][0]) == HexBytes(ORDER_PLACED) for lg in logs)  # not re-placed

    order = await DEX.fns.getOrder(order_id).call(w3)
    assert order[3]  # isBid flipped from ask to bid
    assert order[5] == AMOUNT and order[6] == AMOUNT  # amount kept, remaining reset to full
    assert order[9] and order[10] == 0  # isFlip stays true; flipTick == tick == 0

    # TIP-1056: the flip re-rested under the SAME id -- only the initial place consumed an order id
    assert await DEX.fns.nextOrderId().call(w3) == order_id + 1


async def test_cancel_after_flip_refunds_flipped_side(w3, chain_id, funded_account):
    """TIP-1056: cancelling an ask that flipped to a bid refunds the bid's quote escrow, not the base."""
    maker, _token, order_id, _fill = await _flipped_ask(w3, chain_id, funded_account)

    path_before = await ERC20.fns.balanceOf(maker.address).call(w3, to=PATH_USD)
    assert await DEX.fns.balanceOf(maker.address, PATH_USD).call(w3) == 0
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

    assert "OrderDoesNotExist" in await call_revert(w3, DEX_ADDR, DEX.fns.getOrder(order_id).data)


async def test_bid_same_tick_flip_accepted_and_wrong_side_rejected(w3, chain_id, funded_account):
    """TIP-1030 bid side: flipTick == tick is legal at T5+, flipTick < tick never is."""
    maker = await _trader(w3, chain_id)  # PATH-funded; a bid escrows the quote token
    token = await create_token(w3, chain_id=chain_id, admin=funded_account)

    order_id = await DEX.fns.nextOrderId().call(w3)
    await _approve_and(w3, chain_id, maker, PATH_USD, DEX.fns.placeFlip(token, AMOUNT, True, 0, 0))
    order = await DEX.fns.getOrder(order_id).call(w3)
    assert order[3] and order[9] and order[10] == 0  # a resting same-tick bid flip

    # a bid must flip to an equal-or-higher tick: flipTick -10 < tick 0 -> InvalidBidFlipTick
    reason = await call_revert(w3, DEX_ADDR, DEX.fns.placeFlip(token, AMOUNT, True, 0, -10).data, sender=maker.address)
    assert "InvalidBidFlipTick" in reason or "InvalidFlipTick" in reason


async def test_ask_fliptick_above_tick_reverts(w3, chain_id):
    maker = await _trader(w3, chain_id, token=ALPHA_USD, amount=5_000_000_000)
    # approve on-chain so escrow isn't the first failure in the eth_call below
    await send_call(w3, chain_id, maker, **approve_call(DEX_ADDR, ALPHA_USD))
    # an ask requires flipTick <= tick; flipTick 10 > tick 0 -> InvalidFlipTick
    reason = await call_revert(
        w3, DEX_ADDR, DEX.fns.placeFlip(ALPHA_USD, 1_000_000_000, False, 0, 10).data, sender=maker.address
    )
    assert "InvalidFlipTick" in reason or "0xf88aeb80" in reason


# ---- TIP-1087 book index (T8+) ----
# An order stores its book's 4-byte index into the append-only key vector, not the 32-byte key;
# ``createPair`` sets it for new books and ``setBookIndex`` backfills pre-T8 ones.


async def _new_token(w3, chain_id, admin, salt: bytes = bytes(32)) -> tuple[str, HexBytes]:
    """A fresh TIP-20 quoted in PATH_USD, plus the key its book will get."""
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 10_000_000_000), salt=salt)
    return token, HexBytes(await DEX.fns.pairKey(token, PATH_USD).call(w3))


async def _create_pair(w3, chain_id, admin, salt: bytes = bytes(32)) -> tuple[HexBytes, int]:
    token, key = await _new_token(w3, chain_id, admin, salt)
    await send_call(w3, chain_id, admin, DEX_ADDR, DEX.fns.createPair(token).data)
    is_set, index = await DEX.fns.bookIndexForKey(key).call(w3)
    assert is_set, "createPair did not persist a book index"
    return key, index


async def test_create_pair_persists_a_round_tripping_index(w3, chain_id, funded_account):
    """bookKeyForIndex inverts bookIndexForKey, consecutive pairs take consecutive slots, and
    re-backfilling an indexed book is a no-op success."""
    first_key, first = await _create_pair(w3, chain_id, funded_account, salt=bytes(31) + b"\x01")
    second_key, second = await _create_pair(w3, chain_id, funded_account, salt=bytes(31) + b"\x02")

    assert second == first + 1
    assert await DEX.fns.bookKeyForIndex(first).call(w3) == first_key
    assert await DEX.fns.bookKeyForIndex(second).call(w3) == second_key

    await send_call(w3, chain_id, funded_account, DEX_ADDR, DEX.fns.setBookIndex(second).data)
    assert await DEX.fns.bookIndexForKey(second_key).call(w3) == (True, second)


async def test_placing_an_order_auto_creates_an_indexed_book(w3, chain_id, funded_account):
    """`place` auto-creates a missing pair with an index too, or its orders would fall back to V1 layout."""
    admin = funded_account
    token, key = await _new_token(w3, chain_id, admin)
    assert "PairDoesNotExist" in await call_revert(w3, DEX_ADDR, DEX.fns.bookIndexForKey(key).data)

    await _approve_and(w3, chain_id, admin, token, DEX.fns.place(token, 2_000_000_000, False, 0))

    is_set, index = await DEX.fns.bookIndexForKey(key).call(w3)
    assert is_set
    assert await DEX.fns.bookKeyForIndex(index).call(w3) == key


async def test_book_index_rejects_unknown_books(w3):
    """An unknown key or an index past the end surfaces PairDoesNotExist, not a zero index."""
    for data in (
        DEX.fns.bookIndexForKey(HexBytes(b"\xab" * 32)).data,
        DEX.fns.bookKeyForIndex(UNUSED_INDEX).data,
        DEX.fns.setBookIndex(UNUSED_INDEX).data,
    ):
        assert "PairDoesNotExist" in await call_revert(w3, DEX_ADDR, data)
