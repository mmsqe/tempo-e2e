"""Read every row back out of the contract on a loaded node and compare it with the export.

    .venv/bin/python -m integration_tests.tempo_vs_export [--rpc URL] [--workers N] [--files N]

The Go cross-check stops at the seeded module state, so this is what covers Migrate, the dump and
the loader. Rows that predate the seed go against the fixture the seed replayed instead, never
having been exported. Paths: NVNMCHAIN_EXPORT_DIR, NVNMCHAIN_PRESEED.
"""

import argparse
import gzip
import http.client
import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from eth_abi import decode, encode
from eth_utils import keccak

HERE = Path(__file__).resolve().parent
EXPORT = Path(os.getenv("NVNMCHAIN_EXPORT_DIR", "/private/tmp/from-chain"))
PRESEED = Path(
    os.getenv("NVNMCHAIN_PRESEED")
    or Path.home() / "Documents/crypto/inveniam/x/anchoring/evmlayout/testdata/mainnet-preseed.json"
)
ADDRESS = "0x0000000000000000000000000000000000000a00"
ADMIN = "nvnm14a3em3mr9mvta9ccgk80wn0dxgzt5lkt2r8trx"
SEEDED_AT = "2026-07-30 15:04:00.973906311 +0000 UTC"
FIRST_SEEDED_ID = 69  # the 68 that predate the seed keep 1..68
PAGE_LIMIT = 200
CHUNK = 500  # records per bulk read; 1,000 runs past the 50M call gas cap, so it halves on out of gas
RETRIES = 6  # a minute of backoff, which is longer than a node takes to come back from this load
DETERMINISTIC = "out of gas"  # the one RPC error retrying cannot help; the caller halves instead
BATCH = 8  # bulk reads per request; 8 x 885 KB back is the most worth holding in flight

RECORD = "(string,string,string,string,string,string,uint64,uint64,bool,uint64)"
REGISTRY = "(uint64,string,string,string,string,string)"
PAGE = "(bytes,uint64,uint64,bool,bool)"
REGISTRIES = keccak(text=f"registries(uint64,{PAGE})")[:4]
VERSIONS = keccak(text="versions(uint64,uint64,uint64)")[:4]


def reader_code() -> str:
    """The reader's runtime code, built on first use."""
    artifact = HERE / "reader/out/Reader.sol/Reader.json"
    if not artifact.exists():
        subprocess.run(["forge", "build", "--root", str(HERE / "reader")], check=True, capture_output=True)
    return "0x" + json.loads(artifact.read_text())["deployedBytecode"]["object"].removeprefix("0x")


class RpcError(Exception):
    pass


class Rpc:
    """eth_call over one kept-alive connection. The contract answers a page at a time, half an hour
    for the corpus, so the override lays a bulk reader over its code."""

    def __init__(self, url: str, override: dict | None):
        u = urlparse(url)
        # A URL without a port leaves `http.client` to pick 443 or 80; taking the scheme with it
        # is what keeps an `https://` endpoint from being asked in plain HTTP, which a proxy
        # answers with a redirect page rather than JSON.
        self.connect = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        self.host, self.port, self.path = u.hostname, u.port, u.path or "/"
        self.override = override
        self.conn = None

    def call(self, *datas: bytes) -> list[bytes]:
        """One round trip, however many calls. A remote node spends more per request than per call,
        so batching carries the long tail of small registries; the few big ones are bound by the
        node executing them and gain little."""
        tail = ["latest"] + ([self.override] if self.override else [])
        body = json.dumps(
            [
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "eth_call",
                    "params": [{"to": ADDRESS, "data": "0x" + d.hex()}, *tail],
                }
                for i, d in enumerate(datas)
            ]
        )
        for attempt in range(RETRIES):
            if attempt:
                time.sleep(2**attempt)  # this run is the load that knocked it over, so wait it out
            try:
                if self.conn is None:
                    self.conn = self.connect(self.host, self.port)
                self.conn.request("POST", self.path, body, {"content-type": "application/json"})
                answer = self.conn.getresponse()
                status, last, raw = answer.status, f"{answer.status} {answer.reason}", answer.read()
            except (http.client.HTTPException, OSError) as e:
                self.conn, last = None, repr(e)
                continue
            if status >= 500:
                # A gateway's own page, not the node's: it is down or refusing, and it comes back.
                self.conn = None
                continue
            try:
                out = json.loads(raw)
            except ValueError:
                # Not a node at all: an explorer or a proxy on that port answers with a page, and
                # `--rpc` pointed at it rather than at JSON-RPC. Only the first phase gets here,
                # before any worker exists, so leaving by SystemExit cannot strand the pool.
                raise SystemExit(
                    f"{self.host}:{self.port}{self.path} answered {last} with {len(raw)} bytes, not JSON: {raw[:120]!r}"
                ) from None
            # A batch may answer out of order, so each result goes back to its own id.
            answers: list[bytes] = [b""] * len(datas)
            failed = ""
            for item in out:
                if "error" in item:
                    failed = item["error"]["message"]
                    break
                answers[item["id"]] = bytes.fromhex(item["result"][2:])
            if not failed:
                return answers
            # Anything but `out of gas` is the node's own failure: back off as for a 5xx.
            if DETERMINISTIC in failed:
                raise RpcError(failed)
            last = failed
        # An ordinary exception, which a worker hands back to the pool. A `SystemExit` here kills
        # the worker outright, and `imap_unordered` then waits forever for a result nobody sends.
        raise RpcError(f"{self.host}:{self.port}{self.path} still answering {last} after {RETRIES} tries")


def versions(rpc: Rpc, rid: int, start: int, count: int) -> list:
    """Every version of records start .. start + count - 1, in the contract's own order."""
    return list(decode([RECORD + "[]"], rpc.call(VERSIONS + encode(["uint64"] * 3, [rid, start, count]))[0])[0])


def preseed() -> tuple[list, dict[int, list]]:
    """What mainnet held before the seed, from the fixture the seed replayed. It is protobuf
    JSON, so an empty field is simply absent."""
    fixture = json.loads(PRESEED.read_text())
    registries = sorted(
        (int(r["id"]), r["name"], r.get("description", ""), r["creator"], r["created_at"], r.get("metadata", ""))
        for r in fixture["registries"]
    )
    records: dict[int, list] = {}
    for row in fixture["records"]:
        rid, s = int(row["registry_id"]), row["stored"]
        records.setdefault(rid, []).append(
            (
                s.get("uri", ""),
                s.get("checksum", ""),
                s.get("checksum_algo", ""),
                s.get("metadata", ""),
                s.get("timestamp", ""),
                s.get("status", ""),
                int(s["record_id"]),
                int(s["index"]),
                s.get("is_latest", False),
                rid,
            )
        )
    for rows in records.values():
        rows.sort(key=lambda v: (v[6], v[7]))
    return registries, records


RPC: Rpc
IDS: dict[str, int]


def init_worker(url: str, override: dict, ids: dict[str, int]) -> None:
    global RPC, IDS
    RPC, IDS = Rpc(url, override), ids


def check_file(entry: dict) -> tuple[str, int, list[str]]:
    """Every version of every record in one export file, against the contract."""
    name = entry["registry"]
    rid = IDS[name]
    where = f"registry {rid} ({name})"

    # Record ids follow the first occurrence of each checksum, versions its later ones.
    by_checksum: dict[str, list] = {}
    with gzip.open(EXPORT / entry["file"], "rt") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            by_checksum.setdefault(row["checksum"], []).append(
                (row["uri"], row["checksum"], row["checksumAlgo"], row.get("metadata", ""), row.get("status", ""))
            )
    records = list(by_checksum.values())

    rows, start, chunk = 0, 1, CHUNK
    while start <= len(records):
        spans = []
        while len(spans) < BATCH and start <= len(records):
            count = min(chunk, len(records) - start + 1)
            spans.append((start, count))
            start += count
        try:
            answers = RPC.call(*(VERSIONS + encode(["uint64"] * 3, [rid, s, c]) for s, c in spans))
        except RpcError as e:
            if "out of gas" in str(e) and chunk > 1:
                chunk //= 2
                start = spans[0][0]  # the whole batch goes again, at the smaller chunk
                continue
            raise
        for (at, count), answer in zip(spans, answers):
            got = list(decode([RECORD + "[]"], answer)[0])
            want = []
            for r in range(at, at + count):
                history = records[r - 1]
                for i, (uri, checksum, algo, metadata, status) in enumerate(history, 1):
                    want.append((uri, checksum, algo, metadata, SEEDED_AT, status, r, i, i == len(history), rid))
            if len(got) != len(want):
                span = f"{where} records {at}..{at + count - 1}"
                return name, rows, [f"{span}: contract has {len(got)} versions, the export {len(want)}"]
            for g, w in zip(got, want):
                if g != w:
                    return name, rows, [f"{where} record {w[6]} version {w[7]}:\n  contract {g}\n  export   {w}"]
            rows += len(got)

    if versions(RPC, rid, len(records) + 1, 1):
        return name, rows, [f"{where}: contract has a record {len(records) + 1}, the export {len(records)}"]
    return name, rows, []


def all_registries(rpc: Rpc) -> list:
    """Every registry the contract holds, a page at a time. Ids run 1..count, so a page is a slice."""
    have, offset = [], 0
    while True:
        page = decode(
            [REGISTRY + "[]", "(bytes,uint64)"],
            rpc.call(REGISTRIES + encode(["uint64", PAGE], [0, (b"", offset, PAGE_LIMIT, False, False)]))[0],
        )[0]
        have.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += len(page)
    return have


def check_registries(rpc: Rpc, ids: dict[str, int], exported: list) -> tuple[int, int, int, list[str]]:
    """Every registry, and every record version that predates the seed. The first 68 are the
    fixture's, with their own creator and time; the seed added the rest. Returns the two counts
    compared, how many registries postdate the corpus, and what did not match."""
    old_registries, old_records = preseed()
    want = old_registries + [
        (ids[r["name"]], r["name"], r.get("description", ""), ADMIN, SEEDED_AT, r.get("metadata", "")) for r in exported
    ]
    have = all_registries(rpc)

    bad: list[str] = []
    # Ids run 1..count and a page is a slice, so the corpus is the prefix and anything past it was
    # written after the load — a test suite's, and not the export's to answer for.
    added = len(have) - len(want)
    if added < 0:
        bad.append(f"contract has {len(have)} registries, the export and the fixture {len(want)}")
    for got, w in zip(have, want):
        if got != w:
            bad.append(f"registry {w[0]}: contract {got} expected {w}")

    # Every id up to the seed, so a registry the fixture leaves empty has to be empty here too.
    seen = 0
    for rid in range(1, FIRST_SEEDED_ID):
        held = old_records.get(rid, [])
        got = versions(rpc, rid, 1, CHUNK)
        if got != held:
            bad.append(f"registry {rid} before the seed: contract {got[:1]} fixture {held[:1]}")
        seen += len(held)
    return len(want), seen, max(added, 0), bad


def resumed(path: str) -> dict[str, int]:
    """What an earlier run got through, as the `registry rows` lines it appended."""
    if not path or not Path(path).exists():
        return {}
    done = {}
    for line in Path(path).read_text().splitlines():
        name, _, rows = line.rpartition(" ")
        done[name] = int(rows)
    return done


def check_records(args, override: dict, ids: dict[str, int], files: list) -> int:
    """Every version of every record in each export file, in worker processes. Biggest first, so
    the largest is never left running alone at the end; `--resume` skips files a run got through."""
    expected, count = sum(f["records"] for f in files), len(files)
    passed = resumed(args.resume)
    files = [f for f in files if f["registry"] not in passed]
    before, done = sum(passed.values()), len(passed)
    total = before
    if passed:
        print(f"resuming: {done} files and {before:,} rows already checked", flush=True)
    began = spoke = time.monotonic()
    with multiprocessing.Pool(args.workers, initializer=init_worker, initargs=(args.rpc, override, ids)) as pool:
        for name, rows, problems in pool.imap_unordered(check_file, files):
            if problems:
                pool.terminate()
                print(f"\nMISMATCH after {total:,} rows")
                for line in problems:
                    print("  " + line[:600])
                sys.exit(1)
            total += rows
            done += 1
            if args.resume:
                with open(args.resume, "a") as note:
                    note.write(f"{name} {rows}\n")
            # By the clock, not by the file: the biggest file alone holds a tenth of the corpus,
            # so counting files says nothing for minutes at a time and a stall reads like work.
            if time.monotonic() - spoke >= 15 or done == count:
                spoke = time.monotonic()
                rate = (total - before) / max(spoke - began, 1)
                left = (expected - total) / max(rate, 1) / 60
                print(
                    f"  {done}/{count} files, {total:,} rows, {rate:,.0f} rows/s, ~{left:.1f} min left",
                    flush=True,
                )
    print(f"records: {total:,} rows over {done} files in {(time.monotonic() - began) / 60:.1f} min", flush=True)
    if total != expected:
        print(f"\nread {total:,} rows, the manifest says {expected:,}")
        sys.exit(1)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="http://127.0.0.1:8546")
    ap.add_argument("--workers", type=int, default=0, help="0: a core each locally, 4 over the network")
    ap.add_argument("--files", type=int, default=0, help="sample N files spread across the size order")
    ap.add_argument("--resume", default="", help="a file to note checked registries in, and skip on a rerun")
    args = ap.parse_args()
    # A node sharing the machine wants cores of its own; a shared one wants far less than that.
    # The record phase is 12 million bulk reads, and a dozen workers is enough to take a node down.
    local = urlparse(args.rpc).hostname in ("127.0.0.1", "localhost", "::1")
    args.workers = args.workers or (max(1, (os.cpu_count() or 4) - 4) if local else 4)

    manifest = json.loads((EXPORT / "manifest.json").read_text())
    exported = json.loads((EXPORT / "registries.json").read_text())
    ids = {r["name"]: FIRST_SEEDED_ID + i for i, r in enumerate(exported)}
    override = {ADDRESS: {"code": reader_code()}}

    began = time.monotonic()
    compared, seen, added, bad = check_registries(Rpc(args.rpc, override), ids, exported)
    print(
        f"registries: {compared} compared in {time.monotonic() - began:.0f}s, "
        f"with {seen} record versions from before the seed, "
        f"{added} added since the load, {len(bad)} problems",
        flush=True,
    )
    if bad:
        for line in bad[:10]:
            print("  " + line[:400])
        sys.exit(1)

    files = sorted(manifest["files"], key=lambda f: -f["records"])
    if args.files and args.files < len(files):
        # A spread across the size order rather than the head of it: one file holds a tenth of the
        # corpus and the 20 biggest hold half, so `--files 20` off the top is not a sample of the
        # work, it is most of it. Taking each bucket's middle keeps the order biggest-first.
        step = len(files) / args.files
        files = [files[int((i + 0.5) * step)] for i in range(args.files)]
    check_records(args, override, ids, files)
    print(f"\nevery row the contract holds matches {EXPORT}")


if __name__ == "__main__":
    main()
