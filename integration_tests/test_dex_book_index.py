"""TIP-1087 DEX book index (T8+).

Every orderbook now carries a compact index into the DEX's append-only key vector, so an
order can store a 4-byte index instead of a 32-byte book key (the V2 order layout).
``createPair`` persists the index for new books; ``setBookIndex`` backfills it for books
created before T8, which have none.
"""

import pytest
from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo.constants import PATH_USD
from tempo.constants import STABLECOIN_DEX_ADDRESS as DEX_ADDRESS

from .abi import DEX
from .utils import MAX_UINT, STATE_WRITE_GAS, call_revert, create_token, send_call, send_calls

pytestmark = pytest.mark.tempo

# The DEX starts with no books, so the key vector never grows an index this high.
UNUSED_INDEX = 2**32 - 1


async def _book_index(w3, key) -> tuple[bool, int]:
    return await DEX.fns.bookIndexForKey(key).call(w3, to=DEX_ADDRESS)


async def _key_at(w3, index: int) -> HexBytes:
    return HexBytes(await DEX.fns.bookKeyForIndex(index).call(w3, to=DEX_ADDRESS))


async def _new_token(w3, chain_id, admin, salt: bytes = bytes(32)) -> tuple[str, HexBytes]:
    """A fresh TIP-20 quoted in PATH_USD, plus the key its book will get."""
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 10_000_000_000), salt=salt)
    return token, HexBytes(await DEX.fns.pairKey(token, PATH_USD).call(w3, to=DEX_ADDRESS))


async def _create_pair(w3, chain_id, admin, salt: bytes = bytes(32)) -> tuple[HexBytes, int]:
    token, key = await _new_token(w3, chain_id, admin, salt)
    await send_call(w3, chain_id, admin, DEX_ADDRESS, DEX.fns.createPair(token).data)
    is_set, index = await _book_index(w3, key)
    assert is_set, "createPair did not persist a book index"
    return key, index


async def test_create_pair_persists_a_round_tripping_index(w3, chain_id, funded_account):
    """A book's index is its position in the append-only key vector, so bookKeyForIndex
    inverts bookIndexForKey and consecutive pairs land in consecutive slots. Re-running the
    backfill over an already-indexed book is a no-op success, so it stays safe to call
    blindly across the whole vector."""
    first_key, first = await _create_pair(w3, chain_id, funded_account, salt=bytes(31) + b"\x01")
    second_key, second = await _create_pair(w3, chain_id, funded_account, salt=bytes(31) + b"\x02")

    assert second == first + 1
    assert await _key_at(w3, first) == first_key
    assert await _key_at(w3, second) == second_key

    await send_call(w3, chain_id, funded_account, DEX_ADDRESS, DEX.fns.setBookIndex(second).data)
    assert await _book_index(w3, second_key) == (True, second)


async def test_placing_an_order_auto_creates_an_indexed_book(w3, chain_id, funded_account):
    """`place` auto-creates a missing pair, and that book is indexed too -- otherwise its
    orders would silently fall back to the wider V1 layout."""
    admin = funded_account
    token, key = await _new_token(w3, chain_id, admin)
    assert "PairDoesNotExist" in await call_revert(w3, DEX_ADDRESS, DEX.fns.bookIndexForKey(key).data)

    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": ERC20.fns.approve(DEX_ADDRESS, MAX_UINT).data},
            {"to": DEX_ADDRESS, "data": DEX.fns.place(token, 2_000_000_000, False, 0).data},
        ],
    )
    assert receipt["status"] == 1

    is_set, index = await _book_index(w3, key)
    assert is_set
    assert await _key_at(w3, index) == key


async def test_book_index_rejects_unknown_books(w3):
    """An uninitialized key, and an index past the end of the key vector, both surface
    PairDoesNotExist rather than a zero index."""
    for data in (
        DEX.fns.bookIndexForKey(HexBytes(b"\xab" * 32)).data,
        DEX.fns.bookKeyForIndex(UNUSED_INDEX).data,
        DEX.fns.setBookIndex(UNUSED_INDEX).data,
    ):
        assert "PairDoesNotExist" in await call_revert(w3, DEX_ADDRESS, data)
