"""Replay the old chain's US court data into a node as transactions: the court registries the export lists, in
the old chain's id order and renumbered from 1, then each one's records in write order, so record ids, versions
and checksum lookups come out as the old chain has them. A rerun resumes.

    .venv/bin/python -m integration_tests.replay registries
    .venv/bin/python -m integration_tests.replay ids
    .venv/bin/python -m integration_tests.replay grant [REGISTRY...]
    .venv/bin/python -m integration_tests.replay records [REGISTRY...] [--ramp DATE:PER_DAY,...]
    .venv/bin/python -m integration_tests.replay revoke [REGISTRY...]
    .venv/bin/python -m integration_tests.replay verify [REGISTRY...]

A REGISTRY is a court registry's id here or its name; none means every one. REPLAY_KEY signs. Given
REPLAY_WRITERS, comma-separated addresses, `grant` gives each the editor role on its share of the registries,
balanced by rows, `records` under a writer's key writes only its share, and `revoke` takes the roles back.
Rows come from NVNMCHAIN_EXPORT_DIR, and a court registry the old chain at NVNMCHAIN_API has written to since
stops the run. Records go under one 2D nonce key, each of whose nonces lands once and in order, so a second run
at the same time cannot write one twice. A stop that needs a person exits 3; any other failure is safe to rerun,
and a service may restart it.
"""

import argparse
import csv
import gzip
import itertools
import json
import multiprocessing
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from typing import NoReturn

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak
from tempo import Builder, Signer, serialize, sign_transaction
from tempo.constants import FEE_MANAGER_ADDRESS, NONCE_ADDRESS, PATH_USD
from web3 import HTTPProvider

from .abi import ANCHORING, ANCHORING_ADDRESS, FEE, NONCE
from .anchoring import Page, Record, Registry, bech32
from .tempo_vs_export import ADMIN, CHUNK, EXPORT, FIRST_SEEDED_ID, RECORD, VERSIONS, reader_code

API = os.getenv("NVNMCHAIN_API", "https://api.nvnmchain.io")
SEED_HEIGHT = 2_046_500  # the old chain's block at the export's SEEDED_AT
BASE_FEE_CAP = 12_000_000_000  # TIP-1067's ceiling; a fee under it stalls once full blocks raise the base fee
TX_GAS_CAP = 30_000_000
WINDOW = 512  # unconfirmed transactions kept in the pool, which holds 1,024 of one nonce key by default
NONCE_KEY = int.from_bytes(b"replay")  # the 2D nonce key records go under
WORKERS = max(1, (os.cpu_count() or 4) - 4)  # signers or verifiers, leaving cores to a node on the same machine
PAGE = 200
REPLAYED = {"timestamp", "created_at", "creator"}  # what the replay sets itself
HALT = 3
OFFSET = FIRST_SEEDED_ID - 1  # the old chain's registries before the court ones, which do not carry over


def halt(why: str) -> NoReturn:
    print(why, file=sys.stderr, flush=True)
    raise SystemExit(HALT)


def fetch(path: str, params: dict) -> dict:
    """One answer of the old chain's API, which refuses Python's default User-Agent. Its gateway times out
    when several writers start at once, so a 5xx, a 429 or a timeout is retried for a few minutes."""
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "tempo-e2e"})
    for wait in (5, 10, 20, 40, 80, None):
        try:
            with urllib.request.urlopen(request, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if wait is None or (e.code < 500 and e.code != 429):
                raise
        except (urllib.error.URLError, TimeoutError):
            if wait is None:
                raise
        time.sleep(wait)


def written_since_seed() -> set[int]:
    """The registries the old chain has added a record to or changed one in after the seed height."""
    ids = set()
    for event in ("add_record", "update_record_status"):
        for page in itertools.count(1):  # a page past the last is an error, so `total` ends it
            query = {"query": f"{event}.registry_id EXISTS AND tx.height>{SEED_HEIGHT}", "limit": 100, "page": page}
            answer = fetch("cosmos/tx/v1beta1/txs", query)
            for tx in answer["tx_responses"]:
                for e in tx["events"]:
                    if e["type"] == event:
                        ids |= {int(a["value"]) for a in e["attributes"] if a["key"] == "registry_id"}
            if page * 100 >= int(answer["total"]):
                break
    return ids


def old_registries() -> list[Registry]:
    """Every registry of the old chain, whose JSON leaves empty fields out and quotes integers."""
    rows = []
    while True:
        query = {"pagination.limit": PAGE, "pagination.offset": len(rows)}
        page = fetch("NVNM-Chain/nvnmchain/anchoring/v1/registries", query)["registries"]
        rows += [Registry(*(int(r[f]) if f == "id" else r.get(f, "") for f in Registry._fields)) for r in page]
        if len(page) < PAGE:
            break
    assert [r.id for r in rows] == list(range(1, len(rows) + 1)), "the old chain's ids have a gap"
    return rows


def rows_of(registry: Registry, seeded: dict[int, dict]) -> list[Record]:
    """A registry's records from the export, in write order. No export file repeats a checksum, so each row is
    its own record."""
    rid, entry = registry.id, seeded[registry.id]
    assert entry["registry"] == registry.name, f"registry {rid} is {registry.name}, the export's {entry}"
    with gzip.open(EXPORT / entry["file"], "rt") as f:
        return [
            Record(r["uri"], r["checksum"], r["checksumAlgo"], r["metadata"], "", r["status"], i, 1, True, rid)
            for i, r in enumerate(map(json.loads, f), 1)
        ]


class Node:
    def __init__(self, url: str):
        self.url = url
        self.provider = HTTPProvider(url, request_kwargs={"timeout": 300})
        self.reader = None

    def rpc(self, method: str, *params):
        return self.batch(method, [list(params)])[0]

    def batch(self, method: str, params: list[list]) -> list:
        if not params:  # a node answers an empty batch with one error, not a list
            return []
        replies = self.provider.make_batch_request([(method, p) for p in params])
        for reply in replies:
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
        return [r["result"] for r in replies]

    def nonce(self, address: str, block: str = "latest", nonce_key: int = 0) -> int:
        """`address`'s next nonce at `block`: the protocol's, or a 2D `nonce_key`'s."""
        if not nonce_key:
            return int(self.rpc("eth_getTransactionCount", address, block), 16)
        data = "0x" + NONCE.fns.getNonce(address, nonce_key).data.hex()
        return int(self.rpc("eth_call", {"to": NONCE_ADDRESS, "data": data}, block), 16)

    def lifted(self) -> bool:
        """Whether the general lane is far wider than one transaction, as a rehearsal's is."""
        limit = self.rpc("eth_getBlockByNumber", "latest", False)["mainBlockGeneralGasLimit"]
        return int(limit, 16) >= 100 * TX_GAS_CAP

    def check(self, address: str, after: int, until: int) -> int:
        """Stops on any of `address`'s transactions that failed in blocks after..until, and returns `until`: a
        failed one still took its nonce, so every record sent after it took the wrong id."""
        address = address.lower()
        for receipts in self.batch("eth_getBlockReceipts", [[hex(n)] for n in range(after + 1, until + 1)]):
            for r in receipts:
                if r["status"] != "0x1" and r["from"].lower() == address:
                    halt(f"{r['transactionHash']} failed: rebuild the chain, the ids after it are off")
        return until

    def calls(self, datas: list[bytes], override: dict | None = None) -> list[bytes]:
        """One request of `eth_call`s to the contract, answered in order."""
        tail = ["latest"] + ([override] if override else [])
        outs = self.batch("eth_call", [[{"to": ANCHORING_ADDRESS, "data": "0x" + d.hex()}, *tail] for d in datas])
        if "0x" in outs:  # every write would succeed and be lost, so nothing may be sent
            halt(f"{ANCHORING_ADDRESS} answered nothing: no anchoring implementation is deployed behind it")
        return [bytes.fromhex(out[2:]) for out in outs]

    def call(self, data: bytes, override: dict | None = None) -> bytes:
        return self.calls([data], override)[0]

    def registry_count(self) -> int:
        """`_registryCount`, in slot 3 above the 160-bit `_formerAdmin`."""
        return int(self.rpc("eth_getStorageAt", ANCHORING_ADDRESS, "0x3", "latest"), 16) >> 160

    def record_counts(self, rids: list[int], block: str) -> list[int]:
        """`_recordCount[rid]` of each at `block`, from the mapping at slot 5."""
        slots = ["0x" + keccak(encode(["uint256"] * 2, [rid, 5])).hex() for rid in rids]
        words = self.batch("eth_getStorageAt", [[ANCHORING_ADDRESS, slot, block] for slot in slots])
        return [int(word, 16) for word in words]

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
        contract, the chunk halved whenever a call runs out of gas: 500 long records exceed the call's 50M."""
        self.reader = self.reader or {ANCHORING_ADDRESS: {"code": reader_code()}}
        rows, at, chunk = [], start, CHUNK
        while at < start + count:
            n = min(chunk, start + count - at)
            try:
                rows += decode(
                    [RECORD + "[]"], self.call(VERSIONS + encode(["uint64"] * 3, [rid, at, n]), self.reader)
                )[0]
            except RuntimeError as e:
                if "out of gas" not in str(e) or chunk == 1:
                    raise
                chunk //= 2
                continue
            at += n
        return rows


def gas_bound(record: Record) -> int:
    """At most what adding `record` costs: TIP-1000's 250k for each slot its strings, timestamp, packed integers
    and index entries could newly take, plus 1M; measured gas stays within 200k of the slots alone."""
    lengths = [
        len(s.encode()) for s in (record.uri, record.checksum, record.checksum_algo, record.metadata, record.status)
    ]
    return 250_000 * (sum(0 if n == 0 else 1 if n < 32 else 1 + (n + 31) // 32 for n in lengths) + 7) + 1_000_000


def remaining(registry: Registry, rows: list[Record], done: int):
    """Each of `rows` after the `done` the node holds, as calldata and a gas bound."""
    if done < len(rows):
        print(f"{registry.name}: {done} of {len(rows)} rows here", flush=True)
    yield from ((ANCHORING.fns.addRecord(r).data, gas_bound(r)) for r in rows[done:])


def packed(calls):
    """Consecutive calls grouped while their bounds fit one transaction; order is kept, so the ids they take are
    too."""
    batch, total = [], 0
    for data, bound in calls:
        if batch and total + bound > TX_GAS_CAP:
            yield batch
            batch, total = [], 0
        batch.append(data)
        total += bound
    if batch:
        yield batch


SIGNER: tuple  # a pool worker's signer, chain id, fee, fee token and nonce key


def _signer(key: str, chain_id: int, fee: int, token: str, nonce_key: int) -> None:
    global SIGNER
    SIGNER = Signer(key), chain_id, fee, token, nonce_key


def _sign(job: tuple[int, list[bytes], int]) -> str:
    """A tempo transaction calling the contract once for each calldata, signed in a pool worker."""
    nonce, datas, gas = job
    signer, chain_id, fee, token, nonce_key = SIGNER
    tx = Builder().chain_id(chain_id).gas_limit(gas).max_fee_per_gas(fee).max_priority_fee_per_gas(0)
    tx = tx.nonce(nonce).nonce_key(nonce_key).fee_token(token)
    for data in datas:
        tx.add_call(to=ANCHORING_ADDRESS, value=0, data=data)
    return serialize(sign_transaction(tx.build(), signer))


def split(registries: list[Registry], rows: dict[int, int], k: int) -> list[list[Registry]]:
    """`registries` in k shares of about equal rows, each in id order: the largest goes first, each to the share
    with the fewest rows so far."""
    shares, totals = [[] for _ in range(k)], [0] * k
    for r in sorted(registries, key=lambda r: (-rows[r.id], r.id)):
        i = totals.index(min(totals))
        shares[i].append(r)
        totals[i] += rows[r.id]
    return [sorted(share) for share in shares]


def schedule(spec: str, share: float, done: int):
    """What a writer with `share` of the rows, `done` of them on chain, may have sent by a time: the rows due since
    the first point of `DATE:PER_DAY,...` in UTC, linear between the points and held after the last, so a stop is
    made up. A service's unset variable arrives as an empty spec, which stops rather than send unpaced."""
    try:
        points = sorted(
            (datetime.fromisoformat(when).replace(tzinfo=UTC).timestamp(), float(per_day) * share / 86400)
            for when, per_day in (point.rsplit(":", 1) for point in spec.split(","))
        )
    except ValueError:
        halt(f"--ramp {spec!r} is not DATE:PER_DAY,...")

    def due(now: float) -> float:
        total = max(0.0, now - points[-1][0]) * points[-1][1] - done
        for (t0, r0), (t1, r1) in itertools.pairwise(points):
            if (d := min(now, t1) - t0) > 0:
                total += d * (r0 + (r1 - r0) * d / (2 * (t1 - t0)))
        return total

    return due


def send(node: Node, key: str, calls, per_block: int, label: str, pace=None, nonce_key=0, nonce=None) -> None:
    """Sends the calls in order from one key, at most `per_block` transactions a block, workers signing each round
    while the one before goes out. A lane far wider than one transaction is filled by declared gas anyway, so
    there every transaction declares the cap and packs calls under it by their bounds; a normal lane needs each
    call's estimate to fit several a block. `pace(now)`, the records this run may have sent by `now`, holds the
    calls to it. The transactions take `nonce_key`'s nonces from `nonce` on: by default the protocol's, from its
    next free one."""
    address = Account.from_key(key).address
    chain_id = int(node.rpc("eth_chainId"), 16)
    fee = max(2 * int(node.rpc("eth_gasPrice"), 16), BASE_FEE_CAP)
    fn = FEE.fns.userTokens(address)
    token = fn.decode(
        bytes.fromhex(node.rpc("eth_call", {"to": FEE_MANAGER_ADDRESS, "data": "0x" + fn.data.hex()}, "latest")[2:])
    )
    token = PATH_USD if int(token, 16) == 0 else token
    lifted = node.lifted()
    txs = packed(calls) if lifted else ([data] for data, _ in calls)
    call = {"from": address, "to": ANCHORING_ADDRESS, "nonceKey": hex(nonce_key)}
    nonce = node.nonce(address, "pending") if nonce is None else nonce
    checked = int(node.rpc("eth_blockNumber"), 16)
    last_block, budget, sent, start, signed = None, 0, 0, time.time(), []
    with multiprocessing.Pool(WORKERS, _signer, (key, chain_id, fee, token, nonce_key)) as pool:
        while True:
            block = int(node.rpc("eth_blockNumber"), 16)
            if block != last_block:
                last_block, budget = block, per_block
                checked = node.check(address, checked, block)
            room = min(WINDOW - (nonce - node.nonce(address, nonce_key=nonce_key)), budget)
            if pace:
                room = min(room, int(pace(time.time())) - sent)
            chunk = list(itertools.islice(txs, max(room, 0)))
            if lifted:
                gases = [TX_GAS_CAP] * len(chunk)
            else:
                # Told its nonce: a nonce key's first costs 250k more than its later ones.
                estimates = node.batch(
                    "eth_estimateGas",
                    [[{**call, "data": "0x" + data.hex(), "nonce": hex(nonce + i)}] for i, [data] in enumerate(chunk)],
                )
                gases = [min(TX_GAS_CAP, int(e, 16) * 11 // 10) for e in estimates]
            job = pool.map_async(
                _sign, [(nonce + i, tx, g) for i, (tx, g) in enumerate(zip(chunk, gases))], chunksize=25
            )
            for i in range(0, len(signed), 250):  # the round before, while this one is signed
                node.batch("eth_sendRawTransaction", [[raw] for raw in signed[i : i + 250]])
            rows = sum(map(len, chunk))
            signed, nonce, budget, sent = job.get(), nonce + len(chunk), budget - len(chunk), sent + rows
            if sent // 10_000 != (sent - rows) // 10_000:
                print(f"{label}: {sent} sent, block {block}, {sent / (time.time() - start):.0f}/s", flush=True)
            if chunk:
                continue
            if room > 0:
                break
            time.sleep(0.2 if pace else 0.01)  # no room yet, and the round before has gone out
    while node.nonce(address, nonce_key=nonce_key) < nonce:
        time.sleep(0.2)
    node.check(address, checked, int(node.rpc("eth_blockNumber"), 16))
    print(f"{label}: {sent} confirmed in {time.time() - start:.0f}s", flush=True)


def differ(ours: list, theirs: list, fields: tuple[str, ...]) -> tuple[Counter, int]:
    """How many rows differ in each field, and how many of those differences, plus rows only one side has, are
    not the replay's own."""
    tally = Counter(f for mine, old in zip(ours, theirs) for f, a, b in zip(fields, mine, old) if a != b)
    return tally, abs(len(ours) - len(theirs)) + sum(n for f, n in tally.items() if f not in REPLAYED)


def report(what: str, here: int, there: int, tally: Counter) -> None:
    listed = ", ".join(f"{f} {n}" for f, n in tally.items()) or "none"
    print(f"{what}: {here} here, {there} on the old chain; fields that differ: {listed}", flush=True)


VERIFIER: tuple  # a pool worker's node and the seeded court registries' export entries


def _verifier(url: str, seeded: dict[int, dict]) -> None:
    global VERIFIER
    VERIFIER = Node(url), seeded


def _verified(registry: Registry) -> tuple[Counter, int, int, int, list[str]]:
    """One registry checked in a pool worker: `differ`'s tally and count, the rows here and on the old chain,
    and the checksums its records hold."""
    node, seeded = VERIFIER
    want = rows_of(registry, seeded)
    got = node.versions(registry.id, 1, (want[-1].record_id if want else 0) + 1)
    tally, wrong = differ(got, want, Record._fields)
    return tally, wrong, len(got), len(want), [r.checksum for r in want if r.index == 1]


def verify(node: Node, old: list[Registry], chosen: list[Registry], seeded: dict[int, dict]) -> int:
    """Every registry, the chosen ones' records, and the order a checksum several of them hold lists them in."""
    here = node.registries()
    tally, bad = differ(here, old, Registry._fields)
    report("registries", len(here), len(old), tally)

    ours, theirs, total, held, start = 0, 0, Counter(), {}, time.time()
    with multiprocessing.Pool(WORKERS, _verifier, (node.url, seeded)) as pool:
        checked = zip(chosen, pool.imap(_verified, chosen))
        for i, (registry, (tally, wrong, got, want, checksums)) in enumerate(checked, 1):
            if wrong:
                report(registry.name, got, want, tally)
            bad, ours, theirs, total = bad + wrong, ours + got, theirs + want, total + tally
            for checksum in checksums:
                held.setdefault(checksum, []).append(registry.id)
            if i % 100 == 0 and i < len(chosen):
                print(f"verify: {i} of {len(chosen)} registries, {ours} rows in {time.time() - start:.0f}s", flush=True)
    report(f"records of {len(chosen)} registries", ours, theirs, total)

    # The old chain lists a checksum's registries by id; the contract in the order their first versions landed.
    shared, unordered = [(c, ids) for c, ids in held.items() if len(ids) > 1], 0
    for i in range(0, len(shared), 100):
        chunk = shared[i : i + 100]
        fns = [ANCHORING.fns.records(0, c, 0, 0, Page(limit=50)) for c, _ in chunk]
        for (_, ids), fn, out in zip(chunk, fns, node.calls([fn.data for fn in fns])):
            unordered += [r[9] for r in fn.decode(out)[0] if r[9] in ids] != ids
    print(f"checksums several of them hold: {len(shared)}, listed out of id order: {unordered}", flush=True)
    return bad + unordered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["registries", "ids", "grant", "records", "revoke", "verify"])
    ap.add_argument("registries", nargs="*", help="ids or names, for all but registries and ids; none means all")
    ap.add_argument("--rpc", default=os.getenv("TEMPO_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--per-block", type=int, default=WINDOW, help="new transactions a block, to pace a drip")
    ap.add_argument("--ramp", help="for records: DATE:PER_DAY,... in UTC, the writers' rate together")
    args = ap.parse_args()

    node = Node(args.rpc)
    if args.cmd == "ids":
        out = csv.writer(sys.stdout)
        out.writerow(["id", "name"])
        out.writerows(r[:2] for r in node.registries())
        return
    names = json.loads((EXPORT / "registries.json").read_text())
    court = old_registries()[OFFSET : OFFSET + len(names)]
    assert [r.name for r in court] == [r["name"] for r in names], "the export's registries are not one run of ids"
    old = [r._replace(id=r.id - OFFSET) for r in court]
    chosen = old
    if args.registries:
        found = {arg: [r for r in old if str(r.id) == arg or r.name == arg] for arg in args.registries}
        if wrong := [arg for arg, matches in found.items() if len(matches) != 1]:
            halt(f"not one court registry's id or name: {' '.join(wrong)}")
        # Once each: named twice, a registry's last rows would be sent again and land as second versions.
        chosen = sorted({matches[0] for matches in found.values()})
    files = {f["registry"]: f for f in json.loads((EXPORT / "manifest.json").read_text())["files"]}
    seeded = {i: files[r["name"]] for i, r in enumerate(names, 1)}
    # Rows come from the export alone, so one the old chain added since would be left out.
    if changed := sorted({i - OFFSET for i in written_since_seed()} & seeded.keys()):
        halt("written on the old chain since the export: " + " ".join(seeded[i]["registry"] for i in changed))

    if args.cmd == "verify":
        raise SystemExit(1 if verify(node, old, chosen, seeded) else 0)

    key = os.environ["REPLAY_KEY"]
    address = Account.from_key(key).address
    creator = bech32(address)
    print(f"signing as {creator}" + ("" if creator == ADMIN else f", not {ADMIN}"), flush=True)
    # Registries and roles resume from what landed: wait out this key's pending transactions, which a node
    # restart drops.
    while args.cmd != "records" and node.nonce(address, "pending") > node.nonce(address):
        time.sleep(0.5)
    if args.cmd == "registries":
        have = node.registry_count()
        print(f"{have} registries here, {len(old) - have} to send", flush=True)
        # A registry each to a transaction: there are only a few thousand.
        calls = ((ANCHORING.fns.addRegistry(r.name, r.description, r.metadata).data, TX_GAS_CAP) for r in old[have:])
        send(node, key, calls, args.per_block, "registries")
        return

    writers = [w.lower() for w in os.getenv("REPLAY_WRITERS", "").split(",") if w]
    rows = {r.id: seeded[r.id]["records"] for r in chosen}
    shares = dict(zip(writers, split(chosen, rows, len(writers)))) if writers else {}

    def editing(pairs: list[tuple[int, str]]) -> list[bool]:
        """Whether each writer holds the editor role on each registry."""
        roles = [ANCHORING.fns.hasRole(keccak(f"registry:{rid}:editor".encode()), w).data for rid, w in pairs]
        return [any(held) for held in node.calls(roles)]

    if args.cmd in ("grant", "revoke"):  # skips a role already as asked, so a rerun sends what is left
        pairs = [(r.id, w) for w, share in shares.items() for r in share]
        todo = [pair for pair, held in zip(pairs, editing(pairs)) if held == (args.cmd == "revoke")]
        print(f"{len(todo)} of {len(pairs)} roles to {args.cmd}", flush=True)
        fn = ANCHORING.fns.grantRole if args.cmd == "grant" else ANCHORING.fns.revokeRole
        send(node, key, ((fn(rid, "", w, "editor").data, TX_GAS_CAP) for rid, w in todo), args.per_block, args.cmd)
        return

    if writers:
        if (chosen := shares.get(address.lower())) is None:
            halt(f"{address} is not one of REPLAY_WRITERS")
        if not all(editing([(r.id, address) for r in chosen])):  # else every write would fail on chain
            halt(f"{address} lacks the editor role on part of its share: grant first")
    mine = sum(rows[r.id] for r in chosen)
    # The nonce and every count are read at one block, so a second run signs the same record for the same nonce.
    # Where a transaction is one record they must agree, the one wallet's over every court registry and a writer's
    # over its share: a failed one took a nonce and added nothing. A lifted lane packs records, so a rehearsal
    # relies on `verify`.
    block = node.rpc("eth_blockNumber")
    have = dict(zip(seeded, node.record_counts(list(seeded), block)))
    done = sum(have[r.id] for r in chosen)
    nonce, landed = node.nonce(address, block, NONCE_KEY), done if writers else sum(have.values())
    if nonce != landed and not node.lifted():
        halt(f"{nonce} record transactions but {landed} records: one failed, or something else wrote a record")
    # Each writer takes the ramp in proportion to its rows, so they all finish together. With none left it would
    # only hold the run until it reached the rows on chain.
    pace = schedule(args.ramp, mine / sum(rows.values()), done) if args.ramp is not None and done < mine else None
    due = f"{max(0, pace(time.time())):,.0f} due now" if pace else "unpaced"
    print(f"writing {len(chosen)} registries, {mine} rows, {due}", flush=True)
    calls = itertools.chain.from_iterable(remaining(r, rows_of(r, seeded), have[r.id]) for r in chosen)
    send(node, key, calls, args.per_block, "records", pace, NONCE_KEY, nonce)


if __name__ == "__main__":
    main()
