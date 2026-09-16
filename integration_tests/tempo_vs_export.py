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
        self.host, self.port, self.path = u.hostname, u.port, u.path or "/"
        self.override = override
        self.conn = None

    def call(self, data: bytes) -> bytes:
        params = [{"to": ADDRESS, "data": "0x" + data.hex()}, "latest"]
        if self.override:
            params.append(self.override)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": params})
        for attempt in range(2):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(self.host, self.port)
                self.conn.request("POST", self.path, body, {"content-type": "application/json"})
                out = json.loads(self.conn.getresponse().read())
                break
            except (http.client.HTTPException, OSError):
                self.conn = None
                if attempt:
                    raise
        if "error" in out:
            raise RpcError(out["error"]["message"])
        return bytes.fromhex(out["result"][2:])


def versions(rpc: Rpc, rid: int, start: int, count: int) -> list:
    """Every version of records start .. start + count - 1, in the contract's own order."""
    return list(decode([RECORD + "[]"], rpc.call(VERSIONS + encode(["uint64"] * 3, [rid, start, count])))[0])


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


def check_file(entry: dict) -> tuple[int, list[str]]:
    """Every version of every record in one export file, against the contract."""
    rid = IDS[entry["registry"]]
    where = f"registry {rid} ({entry['registry']})"

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
        count = min(chunk, len(records) - start + 1)
        try:
            got = versions(RPC, rid, start, count)
        except RpcError as e:
            if "out of gas" in str(e) and chunk > 1:
                chunk //= 2
                continue
            raise
        want = []
        for r in range(start, start + count):
            history = records[r - 1]
            for i, (uri, checksum, algo, metadata, status) in enumerate(history, 1):
                want.append((uri, checksum, algo, metadata, SEEDED_AT, status, r, i, i == len(history), rid))
        if len(got) != len(want):
            return rows, [
                f"{where} records {start}..{start + count - 1}: "
                f"contract has {len(got)} versions, the export {len(want)}"
            ]
        for g, w in zip(got, want):
            if g != w:
                return rows, [f"{where} record {w[6]} version {w[7]}:\n  contract {g}\n  export   {w}"]
        rows += len(got)
        start += count

    if versions(RPC, rid, len(records) + 1, 1):
        return rows, [f"{where}: contract has a record {len(records) + 1}, the export {len(records)}"]
    return rows, []


def all_registries(rpc: Rpc) -> list:
    """Every registry the contract holds, a page at a time. Ids run 1..count, so a page is a slice."""
    have, offset = [], 0
    while True:
        page = decode(
            [REGISTRY + "[]", "(bytes,uint64)"],
            rpc.call(REGISTRIES + encode(["uint64", PAGE], [0, (b"", offset, PAGE_LIMIT, False, False)])),
        )[0]
        have.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += len(page)
    return have


def check_registries(rpc: Rpc, ids: dict[str, int], exported: list) -> tuple[int, int, list[str]]:
    """Every registry, and every record version that predates the seed. The first 68 are the
    fixture's, with their own creator and time; the seed added the rest. Returns the two counts
    compared and what did not match."""
    old_registries, old_records = preseed()
    want = old_registries + [
        (ids[r["name"]], r["name"], r.get("description", ""), ADMIN, SEEDED_AT, r.get("metadata", "")) for r in exported
    ]
    have = all_registries(rpc)

    bad: list[str] = []
    if len(have) != len(want):
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
    return len(want), seen, bad


def check_records(args, override: dict, ids: dict[str, int], files: list) -> int:
    """Every version of every record in each export file, in worker processes. Biggest first, so
    the largest is never left running alone at the end."""
    expected = sum(f["records"] for f in files)
    total = done = 0
    began = time.monotonic()
    with multiprocessing.Pool(args.workers, initializer=init_worker, initargs=(args.rpc, override, ids)) as pool:
        for rows, problems in pool.imap_unordered(check_file, files):
            if problems:
                pool.terminate()
                print(f"\nMISMATCH after {total:,} rows")
                for line in problems:
                    print("  " + line[:600])
                sys.exit(1)
            total += rows
            done += 1
            if done % 200 == 0 or done == len(files):
                elapsed = time.monotonic() - began
                rate = total / max(elapsed, 1)
                left = (expected - total) / max(rate, 1) / 60
                print(
                    f"  {done}/{len(files)} files, {total:,} rows, {rate:,.0f} rows/s, ~{left:.1f} min left",
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
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 4), help="the node needs cores too")
    ap.add_argument("--files", type=int, default=0, help="check only the N biggest files")
    args = ap.parse_args()

    manifest = json.loads((EXPORT / "manifest.json").read_text())
    exported = json.loads((EXPORT / "registries.json").read_text())
    ids = {r["name"]: FIRST_SEEDED_ID + i for i, r in enumerate(exported)}
    override = {ADDRESS: {"code": reader_code()}}

    began = time.monotonic()
    compared, seen, bad = check_registries(Rpc(args.rpc, override), ids, exported)
    print(
        f"registries: {compared} compared in {time.monotonic() - began:.0f}s, "
        f"with {seen} record versions from before the seed, {len(bad)} problems",
        flush=True,
    )
    if bad:
        for line in bad[:10]:
            print("  " + line[:400])
        sys.exit(1)

    files = sorted(manifest["files"], key=lambda f: -f["records"])
    check_records(args, override, ids, files[: args.files] if args.files else files)
    print(f"\nevery row the contract holds matches {EXPORT}")


if __name__ == "__main__":
    main()
