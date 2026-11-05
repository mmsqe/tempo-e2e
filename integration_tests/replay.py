"""Replay the old chain's anchoring rows into a node as transactions: every registry in id order, then each
registry's records in write order, so ids, versions and checksum lookups come out as the old chain has them.
A rerun resumes.

    .venv/bin/python -m integration_tests.replay registries
    .venv/bin/python -m integration_tests.replay records [REGISTRY...]
    .venv/bin/python -m integration_tests.replay verify [REGISTRY...]

A REGISTRY is an id or a name only one registry has; none means every one. REPLAY_KEY signs. Seeded court
registries' rows come from NVNMCHAIN_EXPORT_DIR, everything else from the old chain at NVNMCHAIN_API.
"""

import argparse
import bisect
import gzip
import itertools
import json
import os
import time
import urllib.parse
import urllib.request
from collections import Counter

from eth_abi import decode, encode
from eth_account import Account
from web3 import HTTPProvider

from .abi import ANCHORING, ANCHORING_ADDRESS
from .anchoring import Page, Record, Registry, bech32
from .tempo_vs_export import ADMIN, CHUNK, EXPORT, FIRST_SEEDED_ID, RECORD, VERSIONS, reader_code

API = os.getenv("NVNMCHAIN_API", "https://api.nvnmchain.io") + "/NVNM-Chain/nvnmchain/anchoring/v1"
BASE_FEE_CAP = 12_000_000_000  # TIP-1067's ceiling; a fee under it stalls once full blocks raise the base fee
TX_GAS_CAP = 30_000_000
WINDOW = 256  # unconfirmed transactions kept in the pool
PAGE = 200
REPLAYED = {"timestamp", "created_at", "creator"}  # what the replay sets itself


def old_chain(path: str, **params) -> list[dict]:
    """Every row of one of the old chain's lists. Its API refuses Python's default User-Agent."""
    rows = []
    while True:
        query = urllib.parse.urlencode({**params, "pagination.limit": PAGE, "pagination.offset": len(rows)})
        request = urllib.request.Request(f"{API}/{path}?{query}", headers={"User-Agent": "tempo-e2e"})
        with urllib.request.urlopen(request, timeout=60) as r:
            page = json.load(r)[path]
        rows += page
        if len(page) < PAGE:
            return rows


def parsed(cls, row: dict):
    """A `Registry` or `Record` from the old chain's JSON, which leaves empty fields out and quotes integers."""
    return cls(
        *(int(row[f]) if f in ("id", "record_id", "index", "registry_id") else row.get(f, "") for f in cls._fields)
    )


def old_registries() -> list[Registry]:
    rows = [parsed(Registry, r) for r in old_chain("registries")]
    assert [r.id for r in rows] == list(range(1, len(rows) + 1)), "the old chain's ids have a gap"
    return rows


def rows_of(registry: Registry, seeded: dict[int, dict]) -> list[Record]:
    """Every version of a registry's records, in write order. No export file repeats a checksum, so each of
    its rows is its own record."""
    rid, entry = registry.id, seeded.get(registry.id)
    if entry:
        assert entry["registry"] == registry.name, f"registry {rid} is {registry.name}, the export's {entry}"
        with gzip.open(EXPORT / entry["file"], "rt") as f:
            return [
                parsed(
                    Record,
                    {
                        **r,
                        "checksum_algo": r["checksumAlgo"],
                        "record_id": i,
                        "index": 1,
                        "is_latest": True,
                        "registry_id": rid,
                    },
                )
                for i, r in enumerate(map(json.loads, f), 1)
            ]
    rows = []
    for latest in old_chain("records", registry_id=rid):  # the old chain lists only the latest versions
        older = [
            old_chain("records", registry_id=rid, record_id=latest["record_id"], index=i)[0]
            for i in range(1, int(latest["index"]))
        ]
        rows += [parsed(Record, r) for r in older + [latest]]
    return rows


class Node:
    def __init__(self, url: str):
        self.provider = HTTPProvider(url, request_kwargs={"timeout": 300})
        self.reader = None

    def rpc(self, method: str, *params):
        reply = self.provider.make_request(method, list(params))
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def batch(self, method: str, params: list[list]) -> list:
        replies = self.provider.make_batch_request([(method, p) for p in params])
        for reply in replies:
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
        return [r["result"] for r in replies]

    def nonce(self, address: str, block: str = "latest") -> int:
        return int(self.rpc("eth_getTransactionCount", address, block), 16)

    def call(self, data: bytes, override: dict | None = None) -> bytes:
        params = [{"to": ANCHORING_ADDRESS, "data": "0x" + data.hex()}, "latest"] + ([override] if override else [])
        out = self.rpc("eth_call", *params)
        if out == "0x":  # every write would succeed and be lost, so nothing may be sent
            raise SystemExit(f"{ANCHORING_ADDRESS} answered nothing: no anchoring implementation is deployed behind it")
        return bytes.fromhex(out[2:])

    def count(self, view, at_most: int) -> int:
        """How many rows a paged view holds, by bisection: `view(offset)` asks for one row."""

        def held(offset: int) -> bool:
            fn = view(offset)
            return bool(fn.decode(self.call(fn.data))[0])

        return bisect.bisect_left(range(at_most + 1), True, key=lambda offset: not held(offset))

    def registries(self) -> list[Registry]:
        rows = []
        while True:
            fn = ANCHORING.fns.registries(0, Page(offset=len(rows), limit=PAGE))
            page = fn.decode(self.call(fn.data))[0]
            rows += page
            if len(page) < PAGE:
                return rows

    def versions(self, rid: int, start: int, count: int) -> list[tuple]:
        """Every version of records start..start+count-1, through tempo_vs_export's bulk reader laid over the
        contract."""
        self.reader = self.reader or {ANCHORING_ADDRESS: {"code": reader_code()}}
        rows = []
        for at in range(start, start + count, CHUNK):
            data = VERSIONS + encode(["uint64"] * 3, [rid, at, min(CHUNK, start + count - at)])
            rows += decode([RECORD + "[]"], self.call(data, self.reader))[0]
        return rows


def remaining(node: Node, registry: Registry, rows: list[Record]):
    """The calldata of `rows` not yet on the node: whole records by bisection, then the last one's versions."""
    if not rows:
        return
    rid = registry.id
    have = node.count(
        lambda offset: ANCHORING.fns.records(rid, "", 0, 0, Page(offset=offset, limit=1)), rows[-1].record_id
    )
    done = sum(r.record_id < have for r in rows) + len(node.versions(rid, have, 1)) if have else 0
    if done < len(rows):
        print(f"{registry.name}: {done} of {len(rows)} rows here", flush=True)
    yield from (ANCHORING.fns.addRecord(r).data for r in rows[done:])


def send(node: Node, acct, calls, per_block: int, label: str) -> None:
    """Sends each calldata in order from one key, at most `per_block` new ones a block."""
    chain_id = int(node.rpc("eth_chainId"), 16)
    nonce = node.nonce(acct.address, "pending")
    fee = max(2 * int(node.rpc("eth_gasPrice"), 16), BASE_FEE_CAP)
    tx = {"from": acct.address, "to": ANCHORING_ADDRESS}
    fixed = {**tx, "type": 2, "chainId": chain_id, "value": 0, "maxFeePerGas": fee, "maxPriorityFeePerGas": 0}
    calls, last_block, budget, sent, start = iter(calls), None, 0, 0, time.time()
    while True:
        block = int(node.rpc("eth_blockNumber"), 16)
        if block != last_block:
            last_block, budget = block, per_block
        room = min(WINDOW - (nonce - node.nonce(acct.address)), budget)
        if room <= 0:
            time.sleep(0.01)
            continue
        chunk = list(itertools.islice(calls, room))
        if not chunk:
            break
        gases = node.batch("eth_estimateGas", [[{**tx, "data": "0x" + data.hex()}] for data in chunk])
        raws = []
        for i, (data, estimate) in enumerate(zip(chunk, gases)):
            gas = min(TX_GAS_CAP, int(estimate, 16) * 11 // 10)
            signed = acct.sign_transaction({**fixed, "nonce": nonce + i, "data": data, "gas": gas})
            raws.append(["0x" + signed.raw_transaction.hex()])
        node.batch("eth_sendRawTransaction", raws)
        nonce, budget, sent = nonce + len(chunk), budget - len(chunk), sent + len(chunk)
        if sent // 1000 != (sent - len(chunk)) // 1000:
            print(f"{label}: {sent} sent, block {block}, {sent / (time.time() - start):.1f}/s", flush=True)
    while node.nonce(acct.address) < nonce:
        time.sleep(0.2)
    print(f"{label}: {sent} confirmed in {time.time() - start:.0f}s", flush=True)


def differ(ours: list, theirs: list, fields: tuple[str, ...]) -> tuple[Counter, int]:
    """How many rows differ in each field, and how many of those differences, plus rows only one side has, are
    not the replay's own."""
    tally = Counter(f for mine, old in zip(ours, theirs) for f, a, b in zip(fields, mine, old) if a != b)
    return tally, abs(len(ours) - len(theirs)) + sum(n for f, n in tally.items() if f not in REPLAYED)


def report(what: str, here: int, there: int, tally: Counter) -> None:
    listed = ", ".join(f"{f} {n}" for f, n in tally.items()) or "none"
    print(f"{what}: {here} here, {there} on the old chain; fields that differ: {listed}", flush=True)


def verify(node: Node, old: list[Registry], chosen: list[Registry], seeded: dict[int, dict]) -> int:
    """Every registry, the chosen ones' records, and the order a checksum several of them hold lists them in."""
    here = node.registries()
    tally, bad = differ(here, old, Registry._fields)
    report("registries", len(here), len(old), tally)

    ours, theirs, total, held = 0, 0, Counter(), {}
    for registry in chosen:
        want = rows_of(registry, seeded)
        got = node.versions(registry.id, 1, (want[-1].record_id if want else 0) + 1)
        tally, wrong = differ(got, want, Record._fields)
        if wrong:
            report(registry.name, len(got), len(want), tally)
        bad, ours, theirs, total = bad + wrong, ours + len(got), theirs + len(want), total + tally
        for r in want:
            if r.index == 1:
                held.setdefault(r.checksum, []).append(registry.id)
    report(f"records of {len(chosen)} registries", ours, theirs, total)

    # The old chain lists a checksum's registries by id; the contract in the order their first versions landed.
    shared, unordered = [(c, ids) for c, ids in held.items() if len(ids) > 1], 0
    for i in range(0, len(shared), 100):
        chunk = shared[i : i + 100]
        fns = [ANCHORING.fns.records(0, c, 0, 0, Page(limit=50)) for c, _ in chunk]
        outs = node.batch(
            "eth_call", [[{"to": ANCHORING_ADDRESS, "data": "0x" + fn.data.hex()}, "latest"] for fn in fns]
        )
        for (_, ids), fn, out in zip(chunk, fns, outs):
            unordered += [r[9] for r in fn.decode(bytes.fromhex(out[2:]))[0] if r[9] in ids] != ids
    print(f"checksums several of them hold: {len(shared)}, listed out of id order: {unordered}", flush=True)
    return bad + unordered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["registries", "records", "verify"])
    ap.add_argument("registries", nargs="*", help="ids or names, for records and verify; none means all")
    ap.add_argument("--rpc", default=os.getenv("TEMPO_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--per-block", type=int, default=WINDOW, help="new transactions a block, to pace a drip")
    args = ap.parse_args()

    node, old = Node(args.rpc), old_registries()
    chosen = old
    if args.registries:
        chosen = []
        for arg in args.registries:
            (registry,) = [r for r in old if str(r.id) == arg or r.name == arg]  # an id, or a name only one has
            chosen.append(registry)
        chosen.sort()
    files = {f["registry"]: f for f in json.loads((EXPORT / "manifest.json").read_text())["files"]}
    names = json.loads((EXPORT / "registries.json").read_text())
    seeded = {FIRST_SEEDED_ID + i: files[r["name"]] for i, r in enumerate(names)}

    if args.cmd == "verify":
        raise SystemExit(1 if verify(node, old, chosen, seeded) else 0)

    acct = Account.from_key(os.environ["REPLAY_KEY"])
    creator = bech32(acct.address)
    print(f"signing as {creator}" + ("" if creator == ADMIN else f", not {ADMIN}"), flush=True)
    # Resume from what landed: wait out this key's pending transactions, which a node restart drops.
    while node.nonce(acct.address, "pending") > node.nonce(acct.address):
        time.sleep(0.5)
    if args.cmd == "registries":
        have = node.count(lambda offset: ANCHORING.fns.registries(0, Page(offset=offset, limit=1)), len(old))
        print(f"{have} registries here, {len(old) - have} to send", flush=True)
        calls = (ANCHORING.fns.addRegistry(r.name, r.description, r.metadata).data for r in old[have:])
        send(node, acct, calls, args.per_block, "registries")
    else:
        calls = itertools.chain.from_iterable(remaining(node, r, rows_of(r, seeded)) for r in chosen)
        send(node, acct, calls, args.per_block, "records")


if __name__ == "__main__":
    main()
