"""Replay the old chain's anchoring rows into a node as transactions: every registry in id order, then each
registry's records in write order, so ids, versions and checksum lookups come out as the old chain has them.
A rerun resumes.

    .venv/bin/python -m integration_tests.replay registries [--genesis FILE]
    .venv/bin/python -m integration_tests.replay records [REGISTRY...]
    .venv/bin/python -m integration_tests.replay handover [REGISTRY...]
    .venv/bin/python -m integration_tests.replay verify [REGISTRY...]

With --genesis, a chain not yet started gets the registries in its genesis instead, keeping the old chain's
creators and times, and its admins: each creator, but the genesis owner for the old module admin, a multisig
no key derives. REPLAY_KEY administers every registry too, so it can write their records, until `handover`.
A REGISTRY is an id or a name only one registry has; none means every one. REPLAY_KEY signs. Seeded court
registries' rows come from NVNMCHAIN_EXPORT_DIR unless the old chain has written to them since, everything else
from the old chain at NVNMCHAIN_API.
"""

import argparse
import gzip
import itertools
import json
import multiprocessing
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak
from tempo import Builder, Signer, serialize, sign_transaction
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD
from web3 import HTTPProvider

from .abi import ANCHORING, ANCHORING_ADDRESS, FEE
from .anchoring import Page, Record, Registry, bech32, from_bech32
from .tempo_vs_export import ADMIN, CHUNK, EXPORT, FIRST_SEEDED_ID, RECORD, VERSIONS, reader_code

API = os.getenv("NVNMCHAIN_API", "https://api.nvnmchain.io")
SEED_HEIGHT = 2_046_500  # the old chain's block at the export's SEEDED_AT
BASE_FEE_CAP = 12_000_000_000  # TIP-1067's ceiling; a fee under it stalls once full blocks raise the base fee
TX_GAS_CAP = 30_000_000
WINDOW = 8192  # unconfirmed transactions kept in the pool
ROUND = 1000  # transactions signed, then sent, at a time
WORKERS = max(1, (os.cpu_count() or 4) - 4)  # signers or verifiers, leaving cores to a node on the same machine
PAGE = 200
REPLAYED = {"timestamp", "created_at", "creator"}  # what the replay sets itself


def word(n: int) -> bytes:
    return n.to_bytes(32, "big")


def slot(key: bytes, parent: int) -> int:
    """`mapping[key]` of the mapping at slot `parent`: a value key padded to a word, a string key as it is."""
    return int.from_bytes(keccak(key + word(parent)), "big")


def admin_role(rid: int) -> bytes:
    return keccak(f"registry:{rid}:admin".encode())


def fetch(path: str, params: dict) -> dict:
    """One answer of the old chain's API, which refuses Python's default User-Agent."""
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tempo-e2e"}), timeout=60) as r:
        return json.load(r)


def old_chain(path: str, **params) -> list[dict]:
    """Every row of one of the old chain's anchoring lists."""
    rows = []
    while True:
        query = {**params, "pagination.limit": PAGE, "pagination.offset": len(rows)}
        page = fetch(f"NVNM-Chain/nvnmchain/anchoring/v1/{path}", query)[path]
        rows += page
        if len(page) < PAGE:
            return rows


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
                Record(r["uri"], r["checksum"], r["checksumAlgo"], r["metadata"], "", r["status"], i, 1, True, rid)
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

    def nonce(self, address: str, block: str = "latest") -> int:
        return int(self.rpc("eth_getTransactionCount", address, block), 16)

    def check(self, address: str, after: int, until: int) -> int:
        """Stops on any of `address`'s transactions that failed in blocks after..until, and returns `until`: a
        failed one still took its nonce, so every record sent after it took the wrong id."""
        address = address.lower()
        for receipts in self.batch("eth_getBlockReceipts", [[hex(n)] for n in range(after + 1, until + 1)]):
            for r in receipts:
                if r["status"] != "0x1" and r["from"].lower() == address:
                    raise SystemExit(f"{r['transactionHash']} failed: rebuild the chain, the ids after it are off")
        return until

    def calls(self, datas: list[bytes], override: dict | None = None) -> list[bytes]:
        """One request of `eth_call`s to the contract, answered in order."""
        tail = ["latest"] + ([override] if override else [])
        outs = self.batch("eth_call", [[{"to": ANCHORING_ADDRESS, "data": "0x" + d.hex()}, *tail] for d in datas])
        if "0x" in outs:  # every write would succeed and be lost, so nothing may be sent
            raise SystemExit(f"{ANCHORING_ADDRESS} answered nothing: no anchoring implementation is deployed behind it")
        return [bytes.fromhex(out[2:]) for out in outs]

    def call(self, data: bytes, override: dict | None = None) -> bytes:
        return self.calls([data], override)[0]

    def stored(self, at: int) -> int:
        return int(self.rpc("eth_getStorageAt", ANCHORING_ADDRESS, "0x" + word(at).hex(), "latest"), 16)

    def registry_count(self) -> int:
        """`_registryCount`, in slot 3 above the 160-bit `_formerAdmin`."""
        return self.stored(3) >> 160

    def record_count(self, rid: int) -> int:
        """`_recordCount[rid]`, of the mapping at slot 5."""
        return self.stored(slot(word(rid), 5))

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


def remaining(node: Node, registry: Registry, rows: list[Record]):
    """Each of `rows` not yet on the node, as calldata and a gas bound: whole records by their count, then the
    last one's versions."""
    if not rows:
        return
    rid = registry.id
    have = node.record_count(rid)
    done = sum(r.record_id < have for r in rows) + len(node.versions(rid, have, 1)) if have else 0
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


def string_slots(at: int, s: str) -> dict[int, bytes]:
    """A string at slot `at`: under 32 bytes in that slot, twice its length in the last byte; else twice its
    length plus one there and the bytes from keccak(at)."""
    b = s.encode()
    if len(b) < 32:
        return {at: b.ljust(31, b"\0") + bytes([2 * len(b)])}
    data = int.from_bytes(keccak(word(at)), "big")
    return {at: word(2 * len(b) + 1)} | {data + i // 32: b[i : i + 32].ljust(32, b"\0") for i in range(0, len(b), 32)}


def registry_slots(registries: list[Registry], admins: dict[int, set[str]]) -> dict[int, bytes]:
    """What `addRegistry` leaves for each registry, with `admins` holding its admin role: the fields, the role,
    the name index keyed by the ASCII-lowered name, and the count."""
    slots, by_name = {3: word(len(registries) << 160)}, {}
    for r in registries:
        base = slot(word(r.id), 4)
        slots[base] = word(r.id)
        for i, field in enumerate(r[1:], 1):
            slots |= string_slots(base + i, field)
        role, members = admin_role(r.id), admins[r.id]
        slots[slot(role, 0)] = role  # the role administers itself
        for admin in sorted(members):  # in a fixed order, so the file is reproducible
            slots[slot(bytes.fromhex(admin[2:]).rjust(32, b"\0"), slot(role, 1))] = word(1)
        slots[slot(role, 2)] = word(len(members))
        by_name.setdefault(r.name.encode().lower(), []).append(r.id)
    for name, ids in by_name.items():
        length = slot(name, 10)
        slots[length] = word(len(ids))
        data = int.from_bytes(keccak(word(length)), "big")
        for i in range(0, len(ids), 4):  # four ids a word, the first in the low bytes
            slots[data + i // 4] = b"".join(n.to_bytes(8, "big") for n in reversed(ids[i : i + 4])).rjust(32, b"\0")
    return {at: value for at, value in slots.items() if any(value)}


def into_genesis(path: Path, registries: list[Registry], me: str) -> None:
    """Writes the registries into the genesis file's anchoring storage. `me` is lower-cased as the other admins
    are, so where it is the creator it counts once."""
    genesis = json.loads(path.read_text())
    storage = genesis["alloc"][ANCHORING_ADDRESS.lower()].setdefault("storage", {})
    if not (owner := storage.get("0x" + word(11).hex())):  # `_owner`, which a genesis at T12 names
        raise SystemExit(f"{path} names no anchoring owner, so its chain starts before T12")
    owner, me = "0x" + owner[-40:], me.lower()
    module_admin = fetch("NVNM-Chain/nvnmchain/anchoring/v1/params", {})["params"]["admin"]
    admins = {r.id: {me, owner if r.creator == module_admin else from_bech32(r.creator)} for r in registries}
    new = {"0x" + word(at).hex(): "0x" + value.hex() for at, value in registry_slots(registries, admins).items()}
    assert not new.keys() & storage.keys(), f"{path} already holds anchoring rows"
    storage |= new
    path.write_text(json.dumps(genesis, indent=2))
    print(f"{len(registries)} registries into {path} ({len(new)} slots), {me} administering each", flush=True)


SIGNER: tuple  # a pool worker's signer, chain id, fee and fee token


def _signer(key: str, chain_id: int, fee: int, token: str) -> None:
    global SIGNER
    SIGNER = Signer(key), chain_id, fee, token


def _sign(job: tuple[int, list[bytes], int]) -> str:
    """A tempo transaction calling the contract once for each calldata, signed in a pool worker."""
    nonce, datas, gas = job
    signer, chain_id, fee, token = SIGNER
    tx = Builder().chain_id(chain_id).gas_limit(gas).max_fee_per_gas(fee).max_priority_fee_per_gas(0)
    tx = tx.nonce(nonce).nonce_key(0).fee_token(token)
    for data in datas:
        tx.add_call(to=ANCHORING_ADDRESS, value=0, data=data)
    return serialize(sign_transaction(tx.build(), signer))


def send(node: Node, key: str, calls, per_block: int, label: str) -> None:
    """Sends the calls in order from one key, at most `per_block` transactions a block, workers signing each round
    while the one before goes out. A lane far wider than one transaction is filled by declared gas anyway, so
    there every transaction declares the cap and packs calls under it by their bounds; a normal lane needs each
    call's estimate to fit several a block."""
    address = Account.from_key(key).address
    chain_id = int(node.rpc("eth_chainId"), 16)
    fee = max(2 * int(node.rpc("eth_gasPrice"), 16), BASE_FEE_CAP)
    fn = FEE.fns.userTokens(address)
    token = fn.decode(
        bytes.fromhex(node.rpc("eth_call", {"to": FEE_MANAGER_ADDRESS, "data": "0x" + fn.data.hex()}, "latest")[2:])
    )
    token = PATH_USD if int(token, 16) == 0 else token
    lifted = int(node.rpc("eth_getBlockByNumber", "latest", False)["mainBlockGeneralGasLimit"], 16) >= 100 * TX_GAS_CAP
    txs = packed(calls) if lifted else ([data] for data, _ in calls)
    nonce, checked = node.nonce(address, "pending"), int(node.rpc("eth_blockNumber"), 16)
    last_block, budget, sent, start, signed = None, 0, 0, time.time(), []
    with multiprocessing.Pool(WORKERS, _signer, (key, chain_id, fee, token)) as pool:
        while True:
            block = int(node.rpc("eth_blockNumber"), 16)
            if block != last_block:
                last_block, budget = block, per_block
                checked = node.check(address, checked, block)
            room = min(WINDOW - (nonce - node.nonce(address)), budget, ROUND)
            if room <= 0:
                time.sleep(0.01)
                continue
            chunk = list(itertools.islice(txs, room))
            if lifted:
                gases = [TX_GAS_CAP] * len(chunk)
            else:
                estimates = node.batch(
                    "eth_estimateGas",
                    [[{"from": address, "to": ANCHORING_ADDRESS, "data": "0x" + data.hex()}] for [data] in chunk],
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
            if not chunk:
                break
    while node.nonce(address) < nonce:
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
    ap.add_argument("cmd", choices=["registries", "records", "handover", "verify"])
    ap.add_argument("registries", nargs="*", help="ids or names, for all but registries; none means all")
    ap.add_argument("--rpc", default=os.getenv("TEMPO_RPC", "http://127.0.0.1:8545"))
    ap.add_argument("--per-block", type=int, default=WINDOW, help="new transactions a block, to pace a drip")
    ap.add_argument("--genesis", type=Path, help="for registries: write them into this genesis file instead")
    args = ap.parse_args()
    if args.genesis and args.cmd != "registries":
        ap.error("--genesis goes with registries")

    node, old = Node(args.rpc), old_registries()
    if args.genesis:
        return into_genesis(args.genesis, old, Account.from_key(os.environ["REPLAY_KEY"]).address)
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
    for rid in sorted(written_since_seed() & seeded.keys()):  # the export no longer holds what they hold
        print(f"{seeded.pop(rid)['registry']}: written since the export, read from the old chain", flush=True)

    if args.cmd == "verify":
        raise SystemExit(1 if verify(node, old, chosen, seeded) else 0)

    key = os.environ["REPLAY_KEY"]
    address = Account.from_key(key).address
    creator = bech32(address)
    print(f"signing as {creator}" + ("" if creator == ADMIN else f", not {ADMIN}"), flush=True)
    # Resume from what landed: wait out this key's pending transactions, which a node restart drops.
    while node.nonce(address, "pending") > node.nonce(address):
        time.sleep(0.5)
    if args.cmd == "registries":
        have = node.registry_count()
        print(f"{have} registries here, {len(old) - have} to send", flush=True)
        # A registry each to a transaction: there are only a few thousand.
        calls = ((ANCHORING.fns.addRegistry(r.name, r.description, r.metadata).data, TX_GAS_CAP) for r in old[have:])
        send(node, key, calls, args.per_block, "registries")
    elif args.cmd == "handover":  # leave every registry another admin holds; the contract keeps the last one
        roles = [admin_role(r.id) for r in chosen]
        held = node.calls([ANCHORING.fns.hasRole(role, address).data for role in roles])
        counts = node.calls([ANCHORING.fns.roleMemberCount(role).data for role in roles])
        leave = [r.id for r, h, n in zip(chosen, held, counts) if any(h) and int.from_bytes(n, "big") > 1]
        print(f"{len(leave)} registries to leave", flush=True)
        calls = ((ANCHORING.fns.revokeRole(rid, "", address, "admin").data, TX_GAS_CAP) for rid in leave)
        send(node, key, calls, args.per_block, "handover")
    else:
        calls = itertools.chain.from_iterable(remaining(node, r, rows_of(r, seeded)) for r in chosen)
        send(node, key, calls, args.per_block, "records")


if __name__ == "__main__":
    main()
