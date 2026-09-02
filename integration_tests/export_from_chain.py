"""Rebuild a migration export from the chain the corpus still lives on.

python -m integration_tests.export_from_chain --out /tmp/from-chain us-afcca us-almb us-alnb us-idd
NVNM_EXPORT_DIR=/tmp/from-chain pytest integration_tests/test_anchoring_service.py --tidx
"""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

NODE = "https://rpc.nvnmchain.io"
BINARY = "nvnmchaind"
PAGE = 200
WAVE = 8
BACKOFF = (1, 2, 4, 8)


def query(what: str, *flags: str, node: str, binary: str) -> dict:
    argv = [binary, "query", "anchoring", what, "--node", node, "--output", "json", *flags]
    for wait in (*BACKOFF, None):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        if wait:
            time.sleep(wait)
    raise RuntimeError(f"{' '.join(argv[2:])}: {proc.stderr.strip()}")


def paged(what: str, field: str, *flags: str, node: str, binary: str, said: str = "") -> list[dict]:
    """Every page of one query, a wave of offsets at a time; a short page is the end. ``said``
    names what is being counted on stderr, so a long pull is a line that moves."""

    def page_at(offset: int) -> list[dict]:
        answer = query(what, *flags, "--page-limit", str(PAGE), "--page-offset", str(offset), node=node, binary=binary)
        return answer.get(field) or []

    collected: list[dict] = []
    start = 0
    with ThreadPoolExecutor(max_workers=WAVE) as pool:
        while True:
            for page in pool.map(page_at, range(start, start + WAVE * PAGE, PAGE)):
                collected += page
                if len(page) < PAGE:
                    return collected
            start += WAVE * PAGE
            if said:
                print(f"\r{said}: {len(collected)} records...", end="", file=sys.stderr, flush=True)


def rows_of(registry_id: str, *, node: str, binary: str, said: str = "") -> list[dict]:
    """One registry's records in the order the export carries them: the ids the module
    assigned, so a record's versions come oldest first. Sorted here because a wave does not."""
    records = paged("records", "records", "--registry-id", registry_id, node=node, binary=binary, said=said)
    records.sort(key=lambda r: (int(r["record_id"]), int(r["index"])))
    return records


def tranche(name: str, records: list[dict]) -> tuple[bytes, bytes]:
    """One registry's export file, gzipped and plain, written as the export writes it: keys
    alphabetical, no space after either separator, non-ASCII as UTF-8, one row per line in the
    module's order, then gzip at level 9 with a zeroed mtime. That is what makes the digests
    reproduce."""
    lines = [
        json.dumps(
            {
                "checksum": record["checksum"],
                "checksumAlgo": record["checksum_algo"],
                "metadata": record["metadata"],
                "registry": name,
                "status": record.get("status", ""),
                "uri": record["uri"],
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for record in records
    ]
    content = ("\n".join(lines) + "\n").encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as archive:
        archive.write(content)
    return buffer.getvalue(), content


def kept(path: Path, sha256_gz: str) -> bool:
    return path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256_gz


def build(out: Path, names: list[str], *, node: str = NODE, binary: str = BINARY, verify: Path | None = None) -> Path:
    """Write a complete export -- listing, manifest, one file per registry -- for ``names``.

    With ``verify``, every file is checked against that manifest and takes its entry from it,
    path included; one already there with the manifest's digest is kept rather than pulled
    again, which is how a run that stopped resumes.
    """
    expected = {}
    if verify:
        expected = {f["registry"]: f for f in json.loads(verify.read_text())["files"]}
        if unknown := [name for name in names if name not in expected]:
            raise SystemExit(f"{verify} describes no registry named: {', '.join(unknown)}")
    listing = {r["name"]: r for r in paged("registries", "registries", node=node, binary=binary)}
    if missing := [name for name in names if name not in listing]:
        raise SystemExit(f"the chain carries no registry named: {', '.join(missing)}")

    out.mkdir(parents=True, exist_ok=True)
    files = []
    for at, name in enumerate(names, 1):
        where = f"{at}/{len(names)} {name}"
        entry = expected.get(name)
        if entry and kept(out / entry["file"], entry["sha256_gz"]):
            files.append(entry)
            print(f"{where}: kept", file=sys.stderr)
            continue
        records = rows_of(listing[name]["id"], node=node, binary=binary, said=where)
        archive, content = tranche(name, records)
        digests = {
            "sha256_gz": hashlib.sha256(archive).hexdigest(),
            "sha256_uncompressed": hashlib.sha256(content).hexdigest(),
        }
        entry = entry or {
            "registry": name,
            "records": len(records),
            "file": f"from-chain/{name}.jsonl.gz",
            **digests,
        }
        for field, got in digests.items():
            if got != entry[field]:
                raise SystemExit(
                    f"{name}: rebuilt {field} is {got}, {verify.name} says {entry[field]}: "
                    "the chain and the export no longer agree"
                )
        path = out / entry["file"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive)
        files.append(entry)
        verified = ", verified" if name in expected else ""
        print(f"\r{where}: {len(records)} records, {len(archive)} bytes gz{verified}", file=sys.stderr)

    described = [
        {
            "name": name,
            "description": listing[name].get("description", ""),
            "metadata": listing[name].get("metadata", ""),
        }
        for name in names
    ]
    (out / "registries.json").write_text(json.dumps(described, indent=1))
    totals = {"registries": len(files), "records": sum(f["records"] for f in files)}
    (out / "manifest.json").write_text(json.dumps({"totals": totals, "files": files}, indent=1))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "names",
        nargs="*",
        help="registry names, as the corpus knows them (us-wva, ...); omit to rebuild every one --verify names",
    )
    parser.add_argument("--out", type=Path, required=True, help="the export directory to write")
    parser.add_argument("--node", default=NODE, help=f"the chain to read the corpus from (default {NODE})")
    parser.add_argument("--binary", default=BINARY, help="the module's own client (default nvnmchaind)")
    parser.add_argument("--verify", type=Path, help="the export's manifest.json, to check every rebuild against")
    args = parser.parse_args()
    if not args.names:
        if not args.verify:
            parser.error("name the registries to rebuild, or --verify <manifest> to rebuild every one it names")
        args.names = [f["registry"] for f in json.loads(args.verify.read_text())["files"]]
    print(f"wrote {build(args.out, args.names, node=args.node, binary=args.binary, verify=args.verify)}")


if __name__ == "__main__":
    main()
