"""Indexer JSON-RPC: ``eth_getTransactions`` and the ``token_`` namespace.

tempo declares these methods (``crates/node/src/rpc/{eth_ext,token}``) but every
handler returns ``unimplemented``; allegro intends to serve them from a reth ExEx.
The schema in those two modules is therefore the whole specification, and this
module tests against it in three layers:

* **wire** (``indexer-rpc``) -- the methods are registered and the params
  deserialize. Runs against tempo's stubs today, which is the point: it pins the
  contract before the ExEx work starts. ``-32601`` means a method went missing,
  ``-32602`` a param shape drifted; ``-32603 unimplemented`` is a pass.
* **semantic** (``--indexer``) -- answers match an oracle built from plain ``eth_``
  RPC, which every backend already serves. Skipped until a backend implements the
  handlers.
* **differential** (``--tidx``) -- answers also match tidx indexing the same chain,
  so agreement is two implementations concurring rather than a node confirming
  itself. tempo-only: tidx decodes tempo's block shape (it needs the
  ``timestampMillis`` header field) and cannot ingest a plain Ethereum chain, so
  this tier does not cover allegro. See ``tidx.py``.

"Filters" here are the query-filter objects these endpoints take -- unrelated to
``test_filters.py``, which covers ``eth_getLogs`` and the ``eth_newFilter`` API.

These methods take no block tag, so an indexer is only eventually consistent with
the chain: every semantic assertion polls through ``_eventually``.
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
from .utils import ISSUER_ROLE, create_token, new_account

pytestmark = pytest.mark.requires(CAP_INDEXER_RPC)

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

SOME_ADDRESS = "0x0000000000000000000000000000000000000001"
SOME_ROLE = "0x" + "00" * 31 + "01"
INDEXER_LAG = 60.0  # generous: an ExEx commits a block behind, a sidecar polls

# TIP-20 role audit trail; token_getRoleHistory is a projection of this log.
ROLE_MEMBERSHIP_UPDATED = keccak(text="RoleMembershipUpdated(bytes32,address,address,bool)")


# ------------------------------------------------------------------- helpers


async def _raw(w3: AsyncWeb3, method: str, params: list) -> dict:
    return await w3.provider.make_request(method, params)


async def _call(w3: AsyncWeb3, method: str, params: list):
    """``result`` of a successful call; fails the test on any JSON-RPC error."""
    resp = await _raw(w3, method, params)
    assert "error" not in resp, f"{method}{params} failed: {resp['error']}"
    return resp["result"]


def _code(resp: dict) -> int | None:
    return (resp.get("error") or {}).get("code")


def _hex(value) -> str:
    """Hashes and addresses compare as lowercase 0x-strings whichever side they came
    from -- web3 hands back HexBytes, tidx serializes BYTEA as 0x-text."""
    return HexBytes(value).to_0x_hex().lower()


async def _eventually(fn, *, timeout: float = INDEXER_LAG, poll: float = 0.5, what: str = "the indexer to catch up"):
    """Poll ``fn`` until it returns something truthy, then return it.

    Indexer reads have no block tag to pin them to the chain tip, so a value that is
    merely *late* is indistinguishable from one that is wrong until the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        last = await fn()
        if last:
            return last
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what} (last value: {last!r})")
        await asyncio.sleep(poll)


# ------------------------------------------------------- wire contract (stubs OK)

# Minimal well-formed params per endpoint -- enough to reach the handler.
BASE = {
    "eth_getTransactions": {},
    "token_getTokens": {},
    "token_getRoleHistory": {},
    "token_getTokensByAddress": {"address": SOME_ADDRESS},
}

# Every documented filter field, so a rename or a dropped field fails here rather than
# silently returning unfiltered results once a backend implements them.
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

# The pagination envelope, which every endpoint flattens beside its own fields --
# ``TokensByAddressParams`` puts it next to ``address`` rather than nesting it, an easy
# detail to get wrong when reimplementing.
PAGINATION = {"cursor": "cursor-from-a-previous-page", "limit": 10, "sort": {"on": "id", "order": "asc"}}


def _case(method, params, case):
    """One wire case, gated so it only runs where the namespace exists.

    ``eth_getTransactions`` is chain-generic, but the ``token_`` namespace projects
    TIP-20 events, so a backend without TIP-20 (allegro) never registers it. Marking
    per-parameter rather than per-test keeps one table covering both backends.
    """
    marks = [] if method.startswith("eth_") else [pytest.mark.requires(CAP_TEMPO_NATIVE)]
    return pytest.param(method, params, case, marks=marks, id=f"{method}-{case}")


ACCEPTED = [
    *(_case(m, p, "bare") for m, p in BASE.items()),
    *(_case(m, {**p, **PAGINATION}, "pagination") for m, p in BASE.items()),
    *(_case(m, {**BASE[m], "filters": f}, "filters") for m, f in FILTERS.items()),
    # Filters are not deny_unknown_fields, so a client built against a newer schema
    # keeps working against an older node. Locked in deliberately: an implementation
    # that rejected unknown keys would break rolling upgrades.
    *(_case(m, {**p, "filters": {"fieldFromAFutureRelease": 1}}, "unknown-filter") for m, p in BASE.items()),
]

# Params the node must *reject* -- these guard the schema from the other side, so a
# permissive rewrite that accepts anything fails too.
REJECTED = [
    _case("eth_getTransactions", [], "no params at all"),
    _case("eth_getTransactions", [{"limit": "ten"}], "limit must be a number"),
    _case("eth_getTransactions", [{"filters": {"from": "notanaddress"}}], "from must be an address"),
    _case("eth_getTransactions", [{"sort": {"on": "blockNumber", "order": "sideways"}}], "order is asc|desc"),
    _case("token_getTokensByAddress", [{}], "address is required"),
]


@pytest.mark.parametrize("method, params, case", ACCEPTED)
async def test_params_are_accepted(w3, method, params, case):
    """Registered, and the params deserialize -- reaching an ``unimplemented`` stub counts."""
    assert _code(await _raw(w3, method, [params])) not in (METHOD_NOT_FOUND, INVALID_PARAMS), (
        f"{method} did not accept its {case} params"
    )


@pytest.mark.parametrize("method, params, why", REJECTED)
async def test_malformed_params_are_rejected(w3, method, params, why):
    assert _code(await _raw(w3, method, params)) == INVALID_PARAMS, why


# ------------------------------------------------- semantics (needs a live indexer)

backed = pytest.mark.requires(CAP_INDEXER)


async def _send(driver, w3, chain_id, sender) -> dict:
    """One ordinary transaction from ``sender``; returns the receipt.

    Delegated to the driver because what "ordinary" means is backend-specific: tempo
    sends its native AA transaction, allegro a plain EIP-1559 transfer. The indexer
    contract is the same either way, which is the point of testing through it.
    """
    return await driver.send_tx(w3, chain_id, sender)


async def _page(w3, sender, **params) -> dict:
    return await _call(w3, "eth_getTransactions", [{"filters": {"from": sender}, "limit": 100, **params}])


async def _indexed(w3, sender, want: set[str]) -> dict | None:
    """``sender``'s page, once it covers every hash in ``want``; else None."""
    page = await _page(w3, sender)
    return page if want <= {_hex(tx["hash"]) for tx in page["transactions"]} else None


async def _walk(w3: AsyncWeb3, sender: str, *, from_block: int) -> set[str]:
    """Every tx hash sent by ``sender`` from ``from_block`` to the tip, by block walk.

    The oracle for ``eth_getTransactions``: whatever the indexer reports has to be
    exactly what walking the chain finds.
    """
    sender = AsyncWeb3.to_checksum_address(sender)
    hashes = set()
    for number in range(from_block, await w3.eth.block_number + 1):
        block = await w3.eth.get_block(number, full_transactions=True)
        hashes |= {_hex(tx["hash"]) for tx in block["transactions"] if tx["from"] == sender}
    return hashes


@backed
async def test_get_transactions_matches_a_block_walk(driver, w3, chain_id, funded_account):
    """The ``from`` filter is exactly what walking every block finds -- no extras, no
    gaps -- and each row lands in the block its receipt reported."""
    start = await w3.eth.block_number
    receipts = [await _send(driver, w3, chain_id, funded_account) for _ in range(3)]
    blocks = {_hex(r["transactionHash"]): r["blockNumber"] for r in receipts}
    assert await _walk(w3, funded_account.address, from_block=start) == set(blocks)

    page = await _eventually(
        lambda: _indexed(w3, funded_account.address, set(blocks)), what="the three sent txs to be indexed"
    )
    senders = {AsyncWeb3.to_checksum_address(tx["from"]) for tx in page["transactions"]}
    assert senders == {funded_account.address}, "the from filter leaked transactions from other senders"
    for tx in page["transactions"]:
        assert int(tx["blockNumber"], 16) == blocks[_hex(tx["hash"])]


@backed
async def test_cursor_pagination_covers_every_transaction_once(driver, w3, chain_id, funded_account):
    """Walking pages of one must reproduce the single-page result exactly: cursors that
    skip or repeat rows are the classic indexer bug, and only show up under paging."""
    sent = {_hex((await _send(driver, w3, chain_id, funded_account))["transactionHash"]) for _ in range(3)}
    unpaged = await _eventually(lambda: _indexed(w3, funded_account.address, sent), what="three txs to be indexed")
    want = [_hex(tx["hash"]) for tx in unpaged["transactions"]]

    seen, cursor = [], None
    for _ in range(len(want) + 1):  # +1 for the final empty/terminating page
        page = await _page(w3, funded_account.address, limit=1, **({"cursor": cursor} if cursor else {}))
        assert len(page["transactions"]) <= 1, "limit was not honoured"
        seen += [_hex(tx["hash"]) for tx in page["transactions"]]
        cursor = page["nextCursor"]
        if cursor is None:
            break
    else:
        pytest.fail(f"nextCursor never went null after {len(want) + 1} pages")

    assert len(seen) == len(set(seen)), f"pagination returned duplicates: {seen}"
    assert seen == want, "paged order/content diverged from the single-page result"


@backed
async def test_sort_order_reverses_the_result(driver, w3, chain_id, funded_account):
    sent = {_hex((await _send(driver, w3, chain_id, funded_account))["transactionHash"]) for _ in range(2)}
    await _eventually(lambda: _indexed(w3, funded_account.address, sent), what="two txs to be indexed")

    asc = await _page(w3, funded_account.address, sort={"on": "blockNumber", "order": "asc"})
    desc = await _page(w3, funded_account.address, sort={"on": "blockNumber", "order": "desc"})
    blocks = [int(tx["blockNumber"], 16) for tx in asc["transactions"]]
    assert blocks == sorted(blocks)
    assert [int(tx["blockNumber"], 16) for tx in desc["transactions"]] == sorted(blocks, reverse=True)


@backed
async def test_limit_is_capped_at_one_hundred(w3):
    """The documented ceiling. Over-large limits must clamp or fail, never stream the
    whole table -- an unbounded page is a trivial way to knock the node over."""
    resp = await _raw(w3, "eth_getTransactions", [{"limit": 10_000}])
    if "error" in resp:
        assert _code(resp) == INVALID_PARAMS
    else:
        assert len(resp["result"]["transactions"]) <= 100


@backed
@pytest.mark.requires("tempo-native")
async def test_get_tokens_lists_a_created_token(w3, chain_id, funded_account):
    """A fresh TIP-20's indexed row agrees with what the token contract reports."""
    token = await create_token(
        w3, chain_id=chain_id, admin=funded_account, name="IDX", mint=(funded_account.address, 5)
    )

    async def indexed():
        page = await _call(w3, "token_getTokens", [{"filters": {"symbol": "IDX"}, "limit": 100}])
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


@backed
@pytest.mark.requires("tempo-native")
async def test_tokens_by_address_reports_the_on_chain_balance(w3, chain_id, funded_account):
    holder = new_account()
    token = await create_token(w3, chain_id=chain_id, admin=funded_account, name="IDXB", mint=(holder.address, 4_200))

    async def indexed():
        page = await _call(w3, "token_getTokensByAddress", [{"address": holder.address, "limit": 100}])
        return next((t for t in page["tokens"] if AsyncWeb3.to_checksum_address(t["token"]["address"]) == token), None)

    row = await _eventually(indexed, what=f"{holder.address}'s balance of {token} to be indexed")
    assert int(row["balance"], 16) == await ERC20.fns.balanceOf(holder.address).call(w3, to=token) == 4_200


@backed
@pytest.mark.requires("tempo-native")
async def test_role_history_matches_the_role_logs(w3, chain_id, funded_account):
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
        page = await _call(w3, "token_getRoleHistory", [{"filters": {"token": token}, "limit": 100}])
        return page["roleChanges"] or None

    changes = await _eventually(indexed, what=f"role changes for {token} to be indexed")
    issuer_grants = [
        c
        for c in changes
        if HexBytes(c["role"]) == HexBytes(ISSUER_ROLE)
        and c["granted"]
        and AsyncWeb3.to_checksum_address(c["account"]) == funded_account.address
    ]
    assert len(issuer_grants) == len(grants), f"expected {len(grants)} ISSUER_ROLE grants, indexer had {issuer_grants}"
    change = issuer_grants[0]
    assert AsyncWeb3.to_checksum_address(change["token"]) == token
    assert change["blockNumber"] == grants[0]["blockNumber"]
    assert _hex(change["transactionHash"]) == _hex(grants[0]["transactionHash"])


# --------------------------------------------- differential oracle (needs --tidx)


def _sql_literal(value) -> str:
    """A 0x hex string as the PostgreSQL BYTEA literal tidx's columns compare against."""
    return "'\\x" + HexBytes(value).hex() + "'::bytea"


@backed
async def test_get_transactions_agrees_with_tidx(driver, w3, chain_id, funded_account, tidx):
    """The node's answer equals tidx's -- same rows, and the same fields on each row,
    since a filter can select correctly and still project wrongly.

    Stronger than the block walk (a second ingest path concurring, not the node
    confirming itself) and O(1) queries instead of O(blocks).
    """
    receipts = [await _send(driver, w3, chain_id, funded_account) for _ in range(3)]
    tidx.wait_for_block(max(r["blockNumber"] for r in receipts))

    rows = tidx.sql(f'SELECT hash, block_num, idx, type FROM txs WHERE "from" = {_sql_literal(funded_account.address)}')
    expected = {_hex(row["hash"]): row for row in rows}
    assert len(expected) >= 3, f"tidx indexed {len(expected)} txs for the sender, expected at least 3"

    page = await _eventually(
        lambda: _indexed(w3, funded_account.address, set(expected)), what="the node's indexer to reach tidx's height"
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


@backed
async def test_no_transaction_is_missing_from_the_node(w3, tidx):
    """Every tx tidx has, the node's indexer has too. Catches what a single-sender test
    cannot: an indexer that silently drops blocks it was notified about."""
    head = tidx.synced_block()
    senders = tidx.sql(f'SELECT DISTINCT "from" FROM txs WHERE block_num <= {head} LIMIT 5')
    assert senders, "tidx has indexed no transactions yet"

    for entry in senders:
        sender = AsyncWeb3.to_checksum_address(entry["from"])
        rows = tidx.sql(f'SELECT hash FROM txs WHERE "from" = {_sql_literal(sender)} AND block_num <= {head}')
        want = {_hex(row["hash"]) for row in rows}
        await _eventually(
            lambda want=want, sender=sender: _indexed(w3, sender, want),
            what=f"the node's indexer to cover tidx's txs for {sender}",
        )
