"""Indexer JSON-RPC (``eth_getTransactions``, ``token_``): tempo's handlers are stubs, allegro implements them.

No method takes a block tag, so answers are eventually consistent and assertions poll through ``_eventually``.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo.constants import PATH_USD
from web3 import AsyncWeb3

from .drivers.base import CAP_INDEXER, CAP_INDEXER_RPC, CAP_TEMPO_NATIVE
from .utils import ISSUER_ROLE, create_token, new_account, rpc_result

pytestmark = pytest.mark.requires(CAP_INDEXER_RPC)
# A live index actually answers, rather than the handler merely existing.
backed = pytest.mark.requires(CAP_INDEXER)

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

SOME_ADDRESS = "0x0000000000000000000000000000000000000001"
SOME_ROLE = "0x" + "00" * 31 + "01"
INDEXER_LAG = 60.0  # generous: an ExEx commits a block behind, a sidecar polls
# Completeness walk depth: full history grows with every earlier test, and drops are no rarer near the tip.
WALK_BLOCKS = 200

# TIP-20 role audit trail; token_getRoleHistory is a projection of this log.
ROLE_MEMBERSHIP_UPDATED = keccak(text="RoleMembershipUpdated(bytes32,address,address,bool)")


# ------------------------------------------------------------------- helpers


def _code(resp: dict) -> int | None:
    return (resp.get("error") or {}).get("code")


def _hex(value) -> str:
    """A lowercase 0x-string, so web3's HexBytes and tidx's BYTEA text compare equal."""
    return HexBytes(value).to_0x_hex().lower()


async def _eventually(fn, *, timeout: float = INDEXER_LAG, poll: float = 0.5, what: str = "the indexer to catch up"):
    """Poll ``fn`` until it returns something truthy, then return it."""
    deadline = time.monotonic() + timeout
    while True:
        last = await fn()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what} (last value: {last!r})")
        await asyncio.sleep(poll)


# --------------------------------------------------- wire contract parameters

# Minimal well-formed params per endpoint -- enough to reach the handler.
BASE = {
    "eth_getTransactions": {},
    "token_getTokens": {},
    "token_getRoleHistory": {},
    "token_getTokensByAddress": {"address": SOME_ADDRESS},
}

# Every documented filter field, so a renamed or dropped one fails here instead of silently not filtering.
FILTERS = {
    "eth_getTransactions": {"from": SOME_ADDRESS, "to": SOME_ADDRESS, "type": "0x76"},
    "token_getTokens": {
        "currency": "USD",
        "creator": SOME_ADDRESS,
        "createdAt": {"min": 0, "max": 9},
        "name": "n",
        "paused": False,
        "quoteToken": SOME_ADDRESS,
        "supplyCap": {"min": 0, "max": 9},
        "symbol": "S",
        "totalSupply": {"min": 0, "max": 9},
    },
    "token_getRoleHistory": {
        "account": SOME_ADDRESS,
        "blockNumber": {"min": 0, "max": 9},
        "granted": True,
        "role": SOME_ROLE,
        "sender": SOME_ADDRESS,
        "timestamp": {"min": 0, "max": 9},
        "token": SOME_ADDRESS,
    },
    "token_getTokensByAddress": {"symbol": "S"},
}

# The pagination envelope, flattened beside each endpoint's own fields rather than nested.
PAGINATION = {"cursor": "cursor-from-a-previous-page", "limit": 10, "sort": {"on": "id", "order": "asc"}}


def _case(method, params, case):
    """One wire case; a ``token_`` case needs TIP-20, so it is marked per parameter."""
    marks = [] if method.startswith("eth_") else [pytest.mark.requires(CAP_TEMPO_NATIVE)]
    return pytest.param(method, params, case, marks=marks, id=f"{method}-{case}")


ACCEPTED = [
    *(_case(m, p, "bare") for m, p in BASE.items()),
    *(_case(m, {**p, **PAGINATION}, "pagination") for m, p in BASE.items()),
    *(_case(m, {**BASE[m], "filters": f}, "filters") for m, f in FILTERS.items()),
    # Unknown keys are accepted: rejecting them would break newer clients during rolling upgrades.
    *(_case(m, {**p, "filters": {"fieldFromAFutureRelease": 1}}, "unknown-filter") for m, p in BASE.items()),
]

# Params the node must reject, so a rewrite that accepts anything fails too.
REJECTED = [
    _case("eth_getTransactions", [], "no params at all"),
    _case("eth_getTransactions", [{"limit": "ten"}], "limit must be a number"),
    _case("eth_getTransactions", [{"filters": {"from": "notanaddress"}}], "from must be an address"),
    _case("eth_getTransactions", [{"sort": {"on": "blockNumber", "order": "sideways"}}], "order is asc|desc"),
    # `Sort::order` has no serde default, so a sort without it is malformed.
    _case("eth_getTransactions", [{"sort": {"on": "blockNumber"}}], "sort needs an order"),
    _case("token_getTokensByAddress", [{}], "address is required"),
]


class TestWireContract:
    """The methods are registered and their params deserialize; tempo's ``-32603 unimplemented`` passes."""

    @pytest.mark.parametrize("method, params, case", ACCEPTED)
    async def test_params_are_accepted(self, w3, method, params, case):
        assert _code(await w3.provider.make_request(method, [params])) not in (METHOD_NOT_FOUND, INVALID_PARAMS), (
            f"{method} did not accept its {case} params"
        )

    @pytest.mark.parametrize("method, params, why", REJECTED)
    async def test_malformed_params_are_rejected(self, w3, method, params, why):
        assert _code(await w3.provider.make_request(method, params)) == INVALID_PARAMS, why


async def _page(w3, filters: dict, **params) -> dict:
    """One page of ``eth_getTransactions``, as the full response envelope."""
    return await rpc_result(w3, "eth_getTransactions", [{"filters": filters, "limit": 100, **params}])


async def _rows(w3, filters: dict, **params) -> list[dict]:
    """Just the rows: the filter tests assert on selection, not on pagination."""
    return (await _page(w3, filters, **params))["transactions"]


async def _indexed(w3, sender, want: set[str]) -> dict | None:
    """``sender``'s first page, once it covers every hash in ``want``; else None."""
    page = await _page(w3, {"from": sender})
    return page if want <= _hashes(page["transactions"]) else None


async def _covered(w3, sender, want: set[str]) -> set[str] | None:
    """``sender``'s hashes across *every* page, once they cover ``want``; else None. A page caps at
    100, so :func:`_indexed` never sees the rest of a busier sender's history."""
    seen, cursor = set(), None
    while True:
        page = await _page(w3, {"from": sender}, **({"cursor": cursor} if cursor else {}))
        seen |= _hashes(page["transactions"])
        cursor = page["nextCursor"]
        if cursor is None:
            return seen if want <= seen else None


def _recipient(tx) -> str | None:
    """A transaction's checksummed recipient (a tempo tx's first call's), or None for a creation."""
    to = tx.get("to") or next((c.get("to") for c in tx.get("calls") or [] if c.get("to")), None)
    return AsyncWeb3.to_checksum_address(to) if to else None


def _recipient_of(receipt, driver) -> str:
    """The ``to`` of a driver's ordinary transaction, checksummed; skip on a creation."""
    to = receipt.get("to")
    if not to:
        pytest.skip(f"backend {driver.name!r} sends a contract creation; no recipient to filter on")
    return AsyncWeb3.to_checksum_address(to)


async def _walk_txs(w3: AsyncWeb3, *, from_block: int) -> list[dict]:
    """Every transaction from ``from_block`` to the tip, in chain order: the oracle the index must match."""
    txs = []
    for number in range(from_block, await w3.eth.block_number + 1):
        block = await w3.eth.get_block(number, full_transactions=True)
        txs.extend(block["transactions"])
    return txs


def _hashes(txs, predicate=lambda _tx: True) -> set[str]:
    """The hashes of ``txs`` satisfying ``predicate``, comparable against a page."""
    return {_hex(tx["hash"]) for tx in txs if predicate(tx)}


class TestTransactions:
    """``eth_getTransactions`` against an oracle built from plain ``eth_`` RPC."""

    pytestmark = backed

    async def test_get_transactions_matches_a_block_walk(self, driver, w3, chain_id, funded_account):
        """No extras, no gaps, and each row lands in the block its receipt reported."""
        start = await w3.eth.block_number
        sender = AsyncWeb3.to_checksum_address(funded_account.address)
        receipts = [await driver.send_tx(w3, chain_id, funded_account) for _ in range(3)]
        blocks = {_hex(r["transactionHash"]): r["blockNumber"] for r in receipts}

        walked = _hashes(await _walk_txs(w3, from_block=start), lambda tx: tx["from"] == sender)
        assert walked == set(blocks)

        page = await _eventually(lambda: _indexed(w3, sender, set(blocks)), what="the three sent txs to be indexed")
        senders = {AsyncWeb3.to_checksum_address(tx["from"]) for tx in page["transactions"]}
        assert senders == {sender}, "the from filter leaked transactions from other senders"
        for tx in page["transactions"]:
            assert int(tx["blockNumber"], 16) == blocks[_hex(tx["hash"])]

    async def test_cursor_pagination_covers_every_transaction_once(self, driver, w3, chain_id, funded_account):
        """Pages of one reproduce the single-page result: skipped or repeated rows show only under paging."""
        sent = {_hex((await driver.send_tx(w3, chain_id, funded_account))["transactionHash"]) for _ in range(3)}
        unpaged = await _eventually(lambda: _indexed(w3, funded_account.address, sent), what="three txs to be indexed")
        want = [_hex(tx["hash"]) for tx in unpaged["transactions"]]

        seen, cursor = [], None
        for _ in range(len(want) + 1):  # +1 for the final empty/terminating page
            page = await _page(w3, {"from": funded_account.address}, limit=1, **({"cursor": cursor} if cursor else {}))
            assert len(page["transactions"]) <= 1, "limit was not honoured"
            seen += [_hex(tx["hash"]) for tx in page["transactions"]]
            cursor = page["nextCursor"]
            if cursor is None:
                break
        else:
            pytest.fail(f"nextCursor never went null after {len(want) + 1} pages")

        assert len(seen) == len(set(seen)), f"pagination returned duplicates: {seen}"
        assert seen == want, "paged order/content diverged from the single-page result"

    async def test_sort_order_reverses_the_result(self, driver, w3, chain_id, funded_account):
        sent = {_hex((await driver.send_tx(w3, chain_id, funded_account))["transactionHash"]) for _ in range(2)}
        await _eventually(lambda: _indexed(w3, funded_account.address, sent), what="two txs to be indexed")

        asc = await _page(w3, {"from": funded_account.address}, sort={"on": "blockNumber", "order": "asc"})
        desc = await _page(w3, {"from": funded_account.address}, sort={"on": "blockNumber", "order": "desc"})
        blocks = [int(tx["blockNumber"], 16) for tx in asc["transactions"]]
        assert blocks == sorted(blocks)
        assert [int(tx["blockNumber"], 16) for tx in desc["transactions"]] == sorted(blocks, reverse=True)

    async def test_limit_defaults_to_ten(self, driver, w3, chain_id, funded_account):
        """A page that names no limit is 10 rows, the default ``PaginationParams`` documents."""
        sent = {_hex((await driver.send_tx(w3, chain_id, funded_account))["transactionHash"]) for _ in range(11)}
        await _eventually(lambda: _indexed(w3, funded_account.address, sent), what="11 txs to be indexed")

        page = await rpc_result(w3, "eth_getTransactions", [{"filters": {"from": funded_account.address}}])
        assert len(page["transactions"]) == 10, "the default page size is not the documented 10"
        assert page["nextCursor"], "a defaulted page that left rows behind reported no cursor"

    async def test_limit_is_capped_at_one_hundred(self, w3):
        """An over-large limit must clamp or fail; on a chain under 100 txs this bounds rather than proves."""
        resp = await w3.provider.make_request("eth_getTransactions", [{"limit": 10_000}])
        if "error" in resp:
            assert _code(resp) == INVALID_PARAMS
        else:
            assert len(resp["result"]["transactions"]) <= 100

    async def test_to_filter_matches_a_block_walk(self, driver, w3, chain_id, funded_account):
        """The other tests select by ``from``, so only this catches a wrong or swapped ``to``."""
        start = await w3.eth.block_number
        receipts = [await driver.send_tx(w3, chain_id, funded_account) for _ in range(3)]
        sent = {_hex(r["transactionHash"]) for r in receipts}
        target = _recipient_of(receipts[0], driver)

        sender = AsyncWeb3.to_checksum_address(funded_account.address)
        want = _hashes(
            await _walk_txs(w3, from_block=start),
            lambda tx: tx["from"] == sender and _recipient(tx) == target,
        )
        assert want, "the block walk found no transaction to the target address"

        await _eventually(lambda: _indexed(w3, sender, sent), what="the sent txs to be indexed")
        got = await _rows(w3, {"from": sender, "to": target})
        assert _hashes(got) == want, "the to filter and the block walk disagree"

    async def test_type_filter_selects_only_that_type(self, driver, w3, chain_id, funded_account):
        """The type is read off the chain: tempo's ordinary tx is ``0x76``, allegro's ``0x2``."""
        start = await w3.eth.block_number
        receipts = [await driver.send_tx(w3, chain_id, funded_account) for _ in range(2)]
        sent = {_hex(r["transactionHash"]) for r in receipts}

        sender = AsyncWeb3.to_checksum_address(funded_account.address)
        walked = await _walk_txs(w3, from_block=start)
        ours = [tx for tx in walked if _hex(tx["hash"]) in sent]
        assert ours, "the block walk did not find the transactions just sent"
        tx_type = int(ours[0]["type"])

        want = _hashes(walked, lambda tx: tx["from"] == sender and int(tx["type"]) == tx_type)
        await _eventually(lambda: _indexed(w3, sender, sent), what="the sent txs to be indexed")

        got = await _rows(w3, {"from": sender, "type": hex(tx_type)})
        assert _hashes(got) == want, "the type filter and the block walk disagree"

        # A type we did not send must not return the ones we did.
        other = 0 if tx_type != 0 else 2
        excluded = await _rows(w3, {"from": sender, "type": hex(other)})
        assert sent.isdisjoint(_hashes(excluded)), f"type {hex(other)} returned type {hex(tx_type)} txs"

    async def test_filters_intersect_rather_than_union(self, driver, w3, chain_id, funded_account):
        """A handler that ORs its filters, or keeps only the last, passes every single-filter test."""
        receipts = [await driver.send_tx(w3, chain_id, funded_account) for _ in range(2)]
        sent = {_hex(r["transactionHash"]) for r in receipts}
        sender = AsyncWeb3.to_checksum_address(funded_account.address)
        target = _recipient_of(receipts[0], driver)

        await _eventually(lambda: _indexed(w3, sender, sent), what="the sent txs to be indexed")

        both = await _rows(w3, {"from": sender, "to": target})
        assert both, "the intersection is empty but one transaction matches both filters"
        for tx in both:
            assert AsyncWeb3.to_checksum_address(tx["from"]) == sender
            assert _recipient(tx) == target

        # Someone else's sender with our recipient must select nothing of ours.
        stranger = await _rows(w3, {"from": new_account().address, "to": target})
        assert not _hashes(stranger) & sent, "the from filter was ignored when to was also set"

    async def test_every_sender_in_recent_blocks_is_fully_indexed(self, driver, w3, chain_id, funded_account):
        """The tests above inspect only their own txs, so this is what catches a dropped block."""
        receipts = [await driver.send_tx(w3, chain_id, funded_account) for _ in range(2)]
        sent = {_hex(r["transactionHash"]) for r in receipts}
        await _eventually(lambda: _indexed(w3, funded_account.address, sent), what="the sent txs to be indexed")

        head = await w3.eth.block_number
        start = max(0, head - WALK_BLOCKS)
        by_sender: dict[str, set[str]] = {}
        for tx in await _walk_txs(w3, from_block=start):
            by_sender.setdefault(AsyncWeb3.to_checksum_address(tx["from"]), set()).add(_hex(tx["hash"]))
        assert by_sender, f"blocks {start}..{head} hold no transactions to check"

        for sender, want in sorted(by_sender.items()):
            got = await _eventually(
                lambda s=sender, w=want: _covered(w3, s, w),
                what=f"the index to cover {sender}'s {len(want)} transaction(s)",
            )
            assert want <= got, f"{sender}: missing {sorted(want - got)}"


class TestTokenNamespace:
    """The ``token_`` namespace, which projects TIP-20 events."""

    pytestmark = [backed, pytest.mark.requires(CAP_TEMPO_NATIVE)]

    async def test_get_tokens_lists_a_created_token(self, w3, chain_id, funded_account):
        """A fresh TIP-20's indexed row agrees with what the token contract reports."""
        token = await create_token(
            w3, chain_id=chain_id, admin=funded_account, name="IDX", mint=(funded_account.address, 5)
        )

        async def indexed():
            page = await rpc_result(w3, "token_getTokens", [{"filters": {"symbol": "IDX"}, "limit": 100}])
            return next((t for t in page["tokens"] if AsyncWeb3.to_checksum_address(t["address"]) == token), None)

        row = await _eventually(indexed, what=f"token {token} to be indexed")
        assert row["name"] == await ERC20.fns.name().call(w3, to=token)
        assert row["symbol"] == await ERC20.fns.symbol().call(w3, to=token)
        assert int(row["decimals"], 16) == await ERC20.fns.decimals().call(w3, to=token)
        assert int(row["totalSupply"], 16) == await ERC20.fns.totalSupply().call(w3, to=token)
        assert AsyncWeb3.to_checksum_address(row["creator"]) == funded_account.address
        assert AsyncWeb3.to_checksum_address(row["quoteToken"]) == AsyncWeb3.to_checksum_address(PATH_USD)
        assert row["currency"] == "USD"
        assert row["paused"] is False

    async def test_tokens_by_address_reports_the_on_chain_balance(self, w3, chain_id, funded_account):
        holder = new_account()
        token = await create_token(
            w3, chain_id=chain_id, admin=funded_account, name="IDXB", mint=(holder.address, 4_200)
        )

        async def indexed():
            page = await rpc_result(w3, "token_getTokensByAddress", [{"address": holder.address, "limit": 100}])
            return next(
                (t for t in page["tokens"] if AsyncWeb3.to_checksum_address(t["token"]["address"]) == token), None
            )

        row = await _eventually(indexed, what=f"{holder.address}'s balance of {token} to be indexed")
        assert int(row["balance"], 16) == await ERC20.fns.balanceOf(holder.address).call(w3, to=token) == 4_200

    async def test_role_history_matches_the_role_logs(self, w3, chain_id, funded_account):
        """``token_getRoleHistory`` is a projection of RoleMembershipUpdated; the log is the oracle."""
        start = await w3.eth.block_number
        token = await create_token(
            w3, chain_id=chain_id, admin=funded_account, name="IDXR", mint=(funded_account.address, 1)
        )
        logs = await w3.eth.get_logs(
            {"fromBlock": start, "toBlock": "latest", "address": token, "topics": [HexBytes(ROLE_MEMBERSHIP_UPDATED)]}
        )
        grants = [lg for lg in logs if HexBytes(lg["topics"][1]) == HexBytes(ISSUER_ROLE)]
        assert grants, "create_token(mint=…) should have granted ISSUER_ROLE"

        async def indexed():
            page = await rpc_result(w3, "token_getRoleHistory", [{"filters": {"token": token}, "limit": 100}])
            return page["roleChanges"] or None

        changes = await _eventually(indexed, what=f"role changes for {token} to be indexed")
        issuer_grants = [
            c
            for c in changes
            if HexBytes(c["role"]) == HexBytes(ISSUER_ROLE)
            and c["granted"]
            and AsyncWeb3.to_checksum_address(c["account"]) == funded_account.address
        ]
        assert len(issuer_grants) == len(grants), (
            f"expected {len(grants)} ISSUER_ROLE grants, indexer had {issuer_grants}"
        )
        change = issuer_grants[0]
        assert AsyncWeb3.to_checksum_address(change["token"]) == token
        assert change["blockNumber"] == grants[0]["blockNumber"]
        assert _hex(change["transactionHash"]) == _hex(grants[0]["transactionHash"])


def _sql_literal(value) -> str:
    """A 0x hex string as the PostgreSQL BYTEA literal tidx's columns compare against."""
    return "'\\x" + HexBytes(value).hex() + "'::bytea"


class TestTidxDifferential:
    """The node's answers match tidx indexing the same chain; tidx decodes only tempo blocks."""

    pytestmark = backed

    async def test_get_transactions_agrees_with_tidx(self, driver, w3, chain_id, funded_account, tidx):
        """Same rows and same fields: a filter can select correctly and still project wrongly."""
        receipts = [await driver.send_tx(w3, chain_id, funded_account) for _ in range(3)]
        tidx.wait_for_block(max(r["blockNumber"] for r in receipts))

        rows = tidx.sql(
            f'SELECT hash, block_num, idx, type FROM txs WHERE "from" = {_sql_literal(funded_account.address)}'
        )
        expected = {_hex(row["hash"]): row for row in rows}
        assert len(expected) >= 3, f"tidx indexed {len(expected)} txs for the sender, expected at least 3"

        page = await _eventually(
            lambda: _indexed(w3, funded_account.address, set(expected)),
            what="the node's indexer to reach tidx's height",
        )
        got = {_hex(tx["hash"]): tx for tx in page["transactions"]}
        assert set(got) == set(expected), f"node-only={set(got) - set(expected)} tidx-only={set(expected) - set(got)}"
        for hash_, tx in got.items():
            row = expected[hash_]
            assert (int(tx["blockNumber"], 16), int(tx["transactionIndex"], 16), int(tx["type"], 16)) == (
                int(row["block_num"]),
                int(row["idx"]),
                int(row["type"]),
            ), f"node and tidx disagree on {hash_}"

    async def test_no_transaction_is_missing_from_the_node(self, w3, tidx):
        """Catches an indexer that silently drops blocks it was notified about."""
        head = tidx.synced_block()
        senders = tidx.sql(f'SELECT DISTINCT "from" FROM txs WHERE block_num <= {head} LIMIT 5')
        assert senders, "tidx has indexed no transactions yet"

        for entry in senders:
            sender = AsyncWeb3.to_checksum_address(entry["from"])
            rows = tidx.sql(f'SELECT hash FROM txs WHERE "from" = {_sql_literal(sender)} AND block_num <= {head}')
            want = {_hex(row["hash"]) for row in rows}
            await _eventually(
                lambda want=want, sender=sender: _covered(w3, sender, want),
                what=f"the node's indexer to cover tidx's txs for {sender}",
            )
