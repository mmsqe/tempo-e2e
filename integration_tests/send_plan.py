"""Send a migration plan, batched, checking every receipt.

PRIVATE_KEY=0x… python -m integration_tests.send_plan --plan todo.jsonl \\
  --rpc http://127.0.0.1:8545 --chain-id 1337 --factory 0x…
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from eth_account import Account
from web3 import AsyncWeb3

from .registry import deployed_addresses
from .utils import send_calls

MAX_CALLS = 32
GAS_CAP = 30_000_000
BUDGET = 27_000_000  # planned per transaction, headroom under the cap
GAS = {"deploy": 6_105_000, "first": 526_679, "later": 54_107, "status": 269_688}  # measured on a devnet


def cost(step: dict) -> int:
    if step["kind"] == "deploy":
        return GAS["deploy"]
    if step["kind"] == "status":
        return GAS["status"]
    return GAS["first"] if step.get("version", 1) == 1 else GAS["later"]


def batched(steps: list[dict]):
    """Steps grouped into transactions, by both limits at once."""
    batch, planned = [], 0
    for step in steps:
        if batch and (len(batch) == MAX_CALLS or planned + cost(step) > BUDGET):
            yield batch
            batch, planned = [], 0
        batch.append(step)
        planned += cost(step)
    if batch:
        yield batch


def batches(steps: list[dict]):
    """The deploys first and on their own, then the rest."""
    yield from batched([s for s in steps if s["kind"] == "deploy"])
    yield from batched([s for s in steps if s["kind"] != "deploy"])


def target(step: dict, factory: str, deployed: dict[str, str]) -> str:
    """The factory for a deploy; else what `reconcile` stamped, or what this run deployed."""
    if step["kind"] == "deploy":
        return factory
    return step.get("to") or deployed[step["registry"]]


async def send(w3, *, chain_id: int, key: str, factory: str, steps: list[dict]) -> int:
    """Send every batch, stopping at the first receipt with status 0: out of gas is a receipt, not an error."""
    deployed: dict[str, str] = {}
    spent = 0
    for at, batch in enumerate(batches(steps), 1):
        calls = [{"to": target(s, factory, deployed), "data": bytes.fromhex(s["data"][2:])} for s in batch]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=key, calls=calls, gas_limit=GAS_CAP)
        if receipt["status"] != 1:
            raise SystemExit(
                f"tx {at} reverted at {receipt['gasUsed']:,} gas, steps {batch[0]['step']}-{batch[-1]['step']}: "
                "nothing in it landed. Reconcile for a fresh --remaining before resending."
            )
        spent += receipt["gasUsed"]
        if batch[0]["kind"] == "deploy":
            addresses = deployed_addresses(receipt, factory)
            if len(addresses) != len(batch):
                raise SystemExit(f"tx {at}: {len(batch)} deploys announced {len(addresses)} registries")
            deployed.update(zip((s["registry"] for s in batch), addresses, strict=True))
        print(f"\r  tx {at}: {len(batch)} calls, {spent:,} gas so far", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return spent


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True, help="the steps to send, one JSON per line")
    parser.add_argument("--rpc", required=True, help="the chain to send to")
    parser.add_argument("--chain-id", type=int, required=True)
    parser.add_argument("--factory", required=True, help="where a deploy goes")
    parser.add_argument("--dry-run", action="store_true", help="report the batching and stop")
    args = parser.parse_args()

    steps = [json.loads(line) for line in args.plan.read_text().splitlines() if line.strip()]
    kinds = {k: sum(1 for s in steps if s["kind"] == k) for k in ("deploy", "record", "status")}
    print(f"{len(steps)} steps: " + ", ".join(f"{n} {k}" for k, n in kinds.items() if n))
    if not steps:
        return

    if args.dry_run:
        for at, batch in enumerate(batches(steps), 1):
            what = "deploys" if batch[0]["kind"] == "deploy" else "calls"
            print(f"  tx {at}: {len(batch)} {what}, ~{sum(cost(s) for s in batch):,} gas")
        print(f"~{sum(cost(s) for s in steps):,} gas planned, nothing sent")
        return

    key = os.environ.get("PRIVATE_KEY")
    if not key:
        raise SystemExit("PRIVATE_KEY is unset: pass the sending key in the environment, not on the command line")
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(args.rpc))
    try:
        spent = await send(w3, chain_id=args.chain_id, key=key, factory=args.factory, steps=steps)
    finally:
        await w3.provider.disconnect()
    print(f"sent {len(steps)} steps from {Account.from_key(key).address}, {spent:,} gas")


if __name__ == "__main__":
    asyncio.run(main())
