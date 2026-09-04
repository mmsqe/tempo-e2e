"""Send a migration plan, batched, checking every receipt.

PRIVATE_KEY=0x… python -m integration_tests.send_plan --plan todo.jsonl \\
  --rpc http://127.0.0.1:8545 --chain-id 1337 --factory 0x…

`PRIVATE_KEYS`, comma separated, sends from several accounts at once: measured 5x on one
big registry, and the sweet spot moves with the chain's block time.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from eth_account import Account
from tempo import Signer, serialize, sign_transaction
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound, Web3RPCError

from .abi import REGISTRY as REG
from .registry import EDITOR, deployed_addresses
from .utils import DEFAULT_MAX_PRIORITY_FEE_PER_GAS, build_tempo_tx, get_nonce

MAX_CALLS = 32
GAS_CAP = 30_000_000
BUDGET = 27_000_000  # planned per transaction, headroom under the cap
GAS = {"deploy": 7_400_000, "first": 526_679, "later": 54_107, "status": 269_688, "leaves": 320_000}  # devnet
# Multiples of the base fee to bid. `suggested_max_fee` bids two, which a long burst outruns:
# a full block raises the base fee 12.5%, so six of them double it. Overbidding costs only
# balance held while the transaction is out; the base fee is burned at its actual value.
FEE_HEADROOM = 8
# A transaction not mined in this long is re-priced and sent again, up to this many times.
RECEIPT_WAIT = 90.0
ATTEMPTS = 5


def cost(step: dict) -> int:
    if step["kind"] == "deploy":
        return GAS["deploy"]
    if step["kind"] in ("status", "leaves"):
        return GAS[step["kind"]]
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


def shares(steps: list[dict], keys: int) -> list[list[dict]]:
    """The steps dealt round robin, whole registries at a time.

    Used for the deploys, where a registry's sender becomes its admin, so the split has
    to follow registries. Sorted by name, so a resumed run deals the same way.
    """
    names = sorted({s["registry"] for s in steps})
    at = {name: i % keys for i, name in enumerate(names)}
    lots: list[list[dict]] = [[] for _ in range(keys)]
    for step in steps:
        lots[at[step["registry"]]].append(step)
    return lots


def evenly(steps: list[dict], keys: int) -> list[list[dict]]:
    """The steps dealt one at a time, ignoring registries: after the grants, any sender may
    write any of them."""
    lots: list[list[dict]] = [[] for _ in range(keys)]
    for i, step in enumerate(steps):
        lots[i % keys].append(step)
    return lots


def opening(registries: dict[str, str], mine: set[str], others: list[str]) -> list[dict]:
    """Editor at registry scope, for every other sender, on every registry this key deployed.

    Without them only the deployer writes a registry, and the corpus's largest holds a tenth
    of it -- a serial tail no split can shorten.
    """
    return [
        {"to": registries[name], "data": REG.fns.grantRole("", address, EDITOR).data}
        for name in sorted(mine)
        for address in others
    ]


async def send(w3, *, chain_id: int, keys: list[str], factory: str, steps: list[dict]) -> int:
    """Deploys first, one key per registry; then the rest from every key at once.

    The grants between the two are what let the second half ignore registries, so the
    work is dealt evenly however lopsided they are.
    """
    deploys = [s for s in steps if s["kind"] == "deploy"]
    rest = [s for s in steps if s["kind"] != "deploy"]
    mine = [Account.from_key(k).address for k in keys]
    deployed: dict[str, str] = {}

    async def together(lots, run) -> int:
        return sum(await asyncio.gather(*(run(k, lot) for k, lot in zip(keys, lots, strict=True))))

    def sending(k, lot):
        return stream(w3, chain_id=chain_id, key=k, factory=factory, steps=lot, deployed=deployed)

    spent = 0
    if deploys:
        lots = shares(deploys, len(keys))
        spent += await together(lots, sending)
        if len(keys) > 1:
            spent += await together(
                [
                    opening(deployed, {s["registry"] for s in lot}, [a for a in mine if a != me])
                    for lot, me in zip(lots, mine, strict=True)
                ],
                lambda k, payload: granting(w3, chain_id=chain_id, key=k, payload=payload),
            )
    if rest:
        spent += await together(evenly(rest, len(keys)), sending)
    print(file=sys.stderr)
    return spent


async def first_receipt(w3, hashes: list, timeout: float):
    """The receipt of whichever of ``hashes`` is mined first; None if none is within ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tx_hash in hashes:
            try:
                return await w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                pass
        await asyncio.sleep(0.1)
    return None


async def landed(w3, *, chain_id: int, key: str, calls: list[dict]):
    """One transaction, re-priced and sent again at the same nonce until it lands.

    Priced when it is built, a transaction is left behind by a rising base fee, and then it
    holds its sender's nonce: everything queued behind it is refused as an underpriced
    replacement, which is how one stuck transaction stops a run. Every attempt keeps the
    nonce and outbids the last, so the node may mine whichever it kept -- they carry the
    same calls, and any one receipt is the answer.
    """
    sender = Account.from_key(key).address
    nonce = await get_nonce(w3, sender)
    sent, bid, tip = [], 0, 0
    for _ in range(ATTEMPTS):
        base = (await w3.eth.get_block("latest")).get("baseFeePerGas") or 0
        tip = max(DEFAULT_MAX_PRIORITY_FEE_PER_GAS, tip * 2)
        bid = max(base * FEE_HEADROOM + tip, bid * 2)
        tx = build_tempo_tx(
            chain_id=chain_id,
            calls=calls,
            nonce=nonce,
            gas_limit=GAS_CAP,
            max_fee_per_gas=bid,
            max_priority_fee_per_gas=tip,
        )
        try:
            sent.append(await w3.eth.send_raw_transaction(serialize(sign_transaction(tx, Signer(key)))))
        except Web3RPCError as refused:
            # With a bid out, a refusal is the nonce spent or a bump that fell short, and waiting
            # on what is out answers both. With none out, there is nothing to wait for.
            if not sent:
                raise SystemExit(f"{sender[:10]} nonce {nonce} refused: {refused}") from None
        receipt = await first_receipt(w3, sent, RECEIPT_WAIT)
        if receipt is not None:
            return receipt
    raise SystemExit(f"{sender[:10]} nonce {nonce} never landed in {ATTEMPTS} tries")


async def granting(w3, *, chain_id: int, key: str, payload: list[dict]) -> int:
    """The grants, batched: plain calls rather than plan steps, so `batched` does not fit."""
    spent = 0
    for i in range(0, len(payload), MAX_CALLS):
        receipt = await landed(w3, chain_id=chain_id, key=key, calls=payload[i : i + MAX_CALLS])
        if receipt["status"] != 1:
            raise SystemExit(f"granting editor reverted at {receipt['gasUsed']:,} gas")
        spent += receipt["gasUsed"]
    return spent


async def stream(w3, *, chain_id: int, key: str, factory: str, steps: list[dict], deployed: dict) -> int:
    """One key's steps, stopping at the first receipt with status 0: out of gas is a receipt, not an error."""
    spent = 0
    for at, batch in enumerate(batches(steps), 1):
        calls = [{"to": target(s, factory, deployed), "data": bytes.fromhex(s["data"][2:])} for s in batch]
        receipt = await landed(w3, chain_id=chain_id, key=key, calls=calls)
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
        print(f"\r  {Account.from_key(key).address[:10]} tx {at}: {spent:,} gas", end="", file=sys.stderr, flush=True)
    return spent


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True, help="the steps to send, one JSON per line")
    parser.add_argument("--rpc", required=True, help="the chain to send to")
    parser.add_argument("--chain-id", type=int, required=True)
    parser.add_argument("--factory", required=True, help="where a deploy goes")
    parser.add_argument("--dry-run", action="store_true", help="report the batching and stop")
    args = parser.parse_args()

    # Read line by line: a full replay's plan is tens of gigabytes, and `read_text`
    # would hold the whole file as one string before any of it is parsed.
    with args.plan.open() as lines:
        steps = [json.loads(line) for line in lines if line.strip()]
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

    raw = os.environ.get("PRIVATE_KEYS") or os.environ.get("PRIVATE_KEY") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise SystemExit("PRIVATE_KEY is unset: pass the sending key in the environment, not on the command line")
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(args.rpc))
    try:
        spent = await send(w3, chain_id=args.chain_id, keys=keys, factory=args.factory, steps=steps)
    finally:
        await w3.provider.disconnect()
    print(f"sent {len(steps)} steps from {len(keys)} sender(s), {spent:,} gas")


if __name__ == "__main__":
    asyncio.run(main())
