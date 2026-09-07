"""Send a migration plan, batched, checking every receipt.

PRIVATE_KEYS=0x…,0x… python -m integration_tests.send_plan --plan todo.jsonl \\
  --rpc http://127.0.0.1:8545 --chain-id 1337 --factory 0x…

Comma separated, so several accounts send at once, each in its own process: signing and
reading receipts is where a sender's time goes, and one thread doing both for every key
tops out long before the chain does.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from multiprocessing import get_context
from pathlib import Path

from eth_account import Account
from tempo import Signer, serialize, sign_transaction
from web3 import AsyncWeb3
from web3.exceptions import Web3RPCError

from .abi import REGISTRY as REG
from .registry import EDITOR, deployed_addresses
from .utils import DEFAULT_MAX_PRIORITY_FEE_PER_GAS, build_tempo_tx, get_nonce

# The most calls one transaction carries: `MAX_AA_CALLS` in tempo's
# crates/transaction-pool/src/validator.rs, which refuses a 33rd outright. This is what fills
# a batch in practice, whatever room the budgets below leave.
MAX_CALLS = 32
# The most gas one transaction may carry, and what a block keeps for non-payment transactions
# in total. The builder fits them by their limit rather than their use, so a transaction
# carrying the cap is a block to itself; one reserving what it plans shares.
GAS_CAP = 30_000_000
BUDGET = 27_000_000  # planned per transaction, headroom under the cap
# What a batch reserves over what it plans. At a tenth, two transactions of thirty-two first
# versions fit a block. Running out of gas reverts the batch and stops the run, so the margin
# does not go lower.
GAS_HEADROOM = 1.1
CALLDATA_GAS = 16  # per byte, which the per-step costs leave out
# What a step is planned at, above what it was measured at, since a reservation that falls
# short reverts. Over the corpus a record averaged 283k and a status 21k; a later version only
# moves a word, where a first version also creates its version-count slot.
GAS = {"deploy": 5_500_000, "first": 330_000, "later": 60_000, "status": 30_000}
# A record stores its strings, so its cost follows their length. `GAS["first"]` covers the
# replay's median record, 612 bytes of calldata; a rooted one carries the legacy envelope at
# about 900, and thirty-two of those reverted at a 21.3M reservation but passed under the cap.
# That brackets one between 664k and 937k, and this rate is the top of the bracket.
RECORD_BYTES_AT, RECORD_BYTE_GAS = 612, 2_100
# A `leaves` step is mostly state: the precompile creates a slot per chunk, each a peak, and
# one for the count -- TIP-1000's 250k each -- plus the call itself.
FRESH_SLOT, LEAVES_CALL = 250_000, 80_000
# How many registries a batch may span before `GAS`'s averages stop describing it: they average
# a registry's records, where all but the first call finds the tree warm. In the corpus's tail
# of small registries, every call is that first one.
SAME_TREE = 2
# The pool also refuses a transaction past its input limit, 128 KiB by reth's default. Thirty-two
# of the corpus's largest records come to 81 KiB, so this only guards the batch that would run
# past it.
BYTES_BUDGET, CALL_OVERHEAD = 96 * 1024, 64
# Multiples of the base fee to bid. `suggested_max_fee` bids two, which a burst outruns: a full
# block raises the base fee 12.5%, so six of them double it. Overbidding costs only balance held
# while the transaction is out; the base fee is burned at its actual value.
FEE_HEADROOM = 8
# How long a base fee reading is trusted. Staleness is measured in blocks, not transactions: at
# 10ms a block, half a second outran any headroom worth bidding, once one process per key could
# fill blocks a single thread never could.
FEE_CACHE = 0.05
# A transaction not mined in this long is re-priced and sent again, up to this many times.
RECEIPT_WAIT = 90.0
ATTEMPTS = 5
# A send costs one round trip plus however long this sleeps, so against a 10ms chain the
# default of 100ms *is* the rate.
RECEIPT_POLL = float(os.environ.get("RECEIPT_POLL", "0.02"))
# How often a sender reports, in transactions: several processes share one stderr.
REPORT_EVERY = 25


def cost(step: dict) -> int:
    """What a step is planned at, for filling a transaction. The peak slot a leaf may open is
    not in these figures; `reserve` adds it, since the gas limit is what has to cover it.
    """
    if step["kind"] == "deploy":
        return GAS["deploy"]
    if step["kind"] == "leaves":
        # `appendLeaves((bytes32,uint8)[] chunks, …)`: the chunks' length word sits right after
        # the two-word head, so the chunk count is read off the calldata rather than guessed.
        data = bytes.fromhex(step["data"][2:])
        chunks = int.from_bytes(data[4 + 64 : 4 + 96], "big")
        return FRESH_SLOT * (chunks + 1) + LEAVES_CALL
    if step["kind"] == "status":
        return GAS["status"]
    if step.get("version", 1) != 1:
        return GAS["later"]
    over = max(0, len(step["data"]) // 2 - 1 - RECORD_BYTES_AT)
    return GAS["first"] + RECORD_BYTE_GAS * over


def size(step: dict) -> int:
    """The calldata a step puts in a transaction, with the envelope around one call."""
    return len(step["data"]) // 2 - 1 + CALL_OVERHEAD


def reserve(batch: list[dict]) -> int:
    """The gas limit a batch is sent with: what it plans, its calldata, the peak slots its
    appends may open, and `GAS_HEADROOM` over the sum -- so two transactions fit a block
    where the cap lets in one.

    `GAS` averages over one registry's records, where all but the first call finds the tree
    warm, so a batch over more trees than `SAME_TREE` keeps the cap: chunk 21's boundary
    batches put thirty-two calls on as many cold registries and wanted 649k each against the
    330k planned. Within one tree the slots are counted per registry, since the precompile
    creates one the first time a tree reaches a height.
    """
    appends = Counter(s["registry"] for s in batch if s["kind"] in ("record", "status"))
    if any(s["kind"] == "leaves" for s in batch) or len(appends) > SAME_TREE:
        return GAS_CAP
    heights = FRESH_SLOT * sum(n.bit_length() for n in appends.values())
    planned = sum(cost(s) for s in batch) + CALLDATA_GAS * sum(size(s) for s in batch) + heights
    return min(GAS_CAP, int(planned * GAS_HEADROOM))


def batched(steps: list[dict]):
    """Steps grouped into transactions, by every limit at once. The call cap is what fills one
    in practice; the gas budget bounds deploys, which are dear enough to fill one first, and the
    calldata budget guards a batch of unusually large records."""
    batch, planned, carried = [], 0, 0
    for step in steps:
        full = len(batch) == MAX_CALLS or planned + cost(step) > BUDGET or carried + size(step) > BYTES_BUDGET
        if batch and full:
            yield batch
            batch, planned, carried = [], 0, 0
        batch.append(step)
        planned += cost(step)
        carried += size(step)
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


def records(steps: list[dict]) -> list[list[dict]]:
    """Steps grouped by the record they touch, keeping the planner's order within a group.

    One record's steps depend on each other: `updateRecordStatus` reverts unless the record
    is already there, and a later version has to follow the earlier one to be numbered
    right. Only steps of *different* records are free to race.
    """
    lots: list[list[dict]] = []
    at: dict[tuple, int] = {}
    for step in steps:
        key = (step["registry"], step["checksum"]) if step.get("checksum") else None
        if key is not None and key in at:
            lots[at[key]].append(step)
            continue
        if key is not None:
            at[key] = len(lots)
        lots.append([step])
    return lots


def evenly(steps: list[dict], keys: int) -> list[list[dict]]:
    """The steps dealt a record at a time, ignoring registries: after the grants, any sender
    may write any of them."""
    lots: list[list[dict]] = [[] for _ in range(keys)]
    for i, one in enumerate(records(steps)):
        lots[i % keys].extend(one)
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


def send(*, rpc: str, chain_id: int, keys: list[str], factory: str, steps: list[dict]) -> int:
    """Deploys first, one key per registry; then the rest from every key at once.

    The grants between the two are what let the second half ignore registries, so the
    work is dealt evenly however lopsided they are. Each key sends from a process of its
    own, so signing and reading receipts scale with the keys rather than sharing a thread;
    a lot reaches its process pickled, about a sixth of the plan per key for eight, while
    this process keeps the whole plan resident. The first key to fail ends the run: the
    others are stopped where they are, since resending needs a fresh reconcile anyway.
    """
    deploys = [s for s in steps if s["kind"] == "deploy"]
    rest = [s for s in steps if s["kind"] != "deploy"]
    mine = [Account.from_key(k).address for k in keys]
    deployed: dict[str, str] = {}
    spent = 0

    pool = get_context("spawn").Pool(processes=len(keys))
    try:

        def together(job: str, payloads: list) -> None:
            nonlocal spent
            jobs = [
                (rpc, chain_id, key, factory, job, payload, deployed)
                for key, payload in zip(keys, payloads, strict=True)
            ]
            # Unordered, so a failure is raised as soon as it happens rather than once every
            # key ahead of it in the deal has finished its lot.
            for gas, announced in pool.imap_unordered(work, jobs):
                spent += gas
                deployed.update(announced)

        if deploys:
            lots = shares(deploys, len(keys))
            together("steps", lots)
            if len(keys) > 1:
                together(
                    "grants",
                    [
                        opening(deployed, {s["registry"] for s in lot}, [a for a in mine if a != me])
                        for lot, me in zip(lots, mine, strict=True)
                    ],
                )
        if rest:
            together("steps", evenly(rest, len(keys)))
    except BaseException:
        pool.terminate()
        raise
    else:
        pool.close()
    finally:
        pool.join()
    return spent


def work(job: tuple) -> tuple[int, dict]:
    """One key's share of a phase, in its own process: a lot of the plan, or the grants to
    make as they are. Returns the gas it spent and the registries it deployed."""
    rpc, chain_id, key, factory, what, payload, deployed = job

    async def run():
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc))
        try:
            sender = Sender(w3, chain_id, key)
            if what == "grants":
                return await granting(sender, payload), {}
            return await stream(sender, factory=factory, steps=payload, deployed=deployed)
        finally:
            await w3.provider.disconnect()

    try:
        return asyncio.run(run())
    except SystemExit as stopped:
        # As an error rather than an exit: a pool worker that exits is replaced, and the
        # lot it held would never be reported back.
        raise RuntimeError(str(stopped)) from None


class Fees:
    """The base fee, read again only when the last reading is older than `FEE_CACHE`."""

    def __init__(self, w3):
        self.w3, self.base, self.read_at = w3, 0, float("-inf")

    def stale(self) -> None:
        """Drop the reading, for a sender the chain has just refused as underpriced."""
        self.read_at = float("-inf")

    async def base_fee(self) -> int:
        if time.monotonic() - self.read_at > FEE_CACHE:
            resp = await self.w3.provider.make_request("eth_getBlockByNumber", ["latest", False])
            if resp.get("error") or not resp.get("result"):
                raise RuntimeError(f"eth_getBlockByNumber latest: {resp.get('error') or 'no block'}")
            self.base = int(resp["result"].get("baseFeePerGas") or "0x0", 16)
            self.read_at = time.monotonic()
        return self.base


async def receipt_of(w3, tx_hash) -> dict | None:
    """The receipt as the node sends it, or None while the transaction is unmined.

    Raw rather than through `w3.eth`: a 32-call receipt is a hundred kilobytes of logs, and
    web3's formatting of it costs more than the round trip. Only `status`, `gasUsed` and the
    logs are read, and `deployed_addresses` takes the logs as they come.
    """
    hexed = tx_hash if isinstance(tx_hash, str) else "0x" + bytes(tx_hash).hex()
    resp = await w3.provider.make_request("eth_getTransactionReceipt", [hexed])
    if resp.get("error"):
        raise RuntimeError(f"eth_getTransactionReceipt {hexed[:10]}: {resp['error']}")
    receipt = resp.get("result")
    if receipt is None:
        return None
    return {"status": int(receipt["status"], 16), "gasUsed": int(receipt["gasUsed"], 16), "logs": receipt["logs"]}


async def first_receipt(w3, hashes: list, timeout: float):
    """The receipt of whichever of ``hashes`` is mined first; None if none is within ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tx_hash in hashes:
            receipt = await receipt_of(w3, tx_hash)
            if receipt is not None:
                return receipt
        await asyncio.sleep(RECEIPT_POLL)
    return None


class Sender:
    """One key's transactions, in nonce order, each landed before the next is built. The
    nonce is read once and counted from there: the chain only ever agrees with it, since
    nothing else sends from the key."""

    def __init__(self, w3, chain_id: int, key: str):
        self.w3, self.chain_id = w3, chain_id
        self.signer = Signer(key)
        self.address = self.signer.checksum_address
        self.fees = Fees(w3)
        self.nonce: int | None = None

    async def landed(self, calls: list[dict], gas_limit: int = GAS_CAP):
        """One transaction, re-priced and sent again at the same nonce until it lands.

        Priced when it is built, a transaction is left behind by a rising base fee, and then
        it holds its sender's nonce: everything queued behind it is refused as an underpriced
        replacement, which is how one stuck transaction stops a run. Every attempt keeps the
        nonce and outbids the last, so the node may mine whichever it kept -- they carry the
        same calls, and any one receipt is the answer.
        """
        if self.nonce is None:
            self.nonce = await get_nonce(self.w3, self.address)
        nonce = self.nonce
        sent, bid, tip, last = [], 0, 0, None
        for _ in range(ATTEMPTS):
            base = await self.fees.base_fee()
            tip = max(DEFAULT_MAX_PRIORITY_FEE_PER_GAS, tip * 2)
            bid = max(base * FEE_HEADROOM + tip, bid * 2)
            tx = build_tempo_tx(
                chain_id=self.chain_id,
                calls=calls,
                nonce=nonce,
                gas_limit=gas_limit,
                max_fee_per_gas=bid,
                max_priority_fee_per_gas=tip,
            )
            try:
                sent.append(await self.w3.eth.send_raw_transaction(serialize(sign_transaction(tx, self.signer))))
            except Web3RPCError as refused:
                # With a bid out, a refusal is the nonce spent or a bump that fell short, and
                # waiting on what is out answers both. With none out, the bid was priced off a
                # reading the chain has left behind: drop it and bid again.
                last = refused
                if not sent:
                    self.fees.stale()
                    continue
            receipt = await first_receipt(self.w3, sent, RECEIPT_WAIT)
            if receipt is not None:
                self.nonce = nonce + 1
                return receipt
        why = f"refused: {last}" if not sent else f"never landed in {ATTEMPTS} tries"
        raise SystemExit(f"{self.address[:10]} nonce {nonce} {why}")


async def granting(sender: Sender, payload: list[dict]) -> int:
    """The grants, batched: plain calls rather than plan steps, so `batched` does not fit."""
    spent = 0
    for i in range(0, len(payload), MAX_CALLS):
        receipt = await sender.landed(payload[i : i + MAX_CALLS])
        if receipt["status"] != 1:
            raise SystemExit(f"granting editor reverted at {receipt['gasUsed']:,} gas")
        spent += receipt["gasUsed"]
    return spent


async def stream(sender: Sender, *, factory: str, steps: list[dict], deployed: dict) -> tuple[int, dict]:
    """One key's steps, stopping at the first receipt with status 0: out of gas is a receipt,
    not an error. Returns the gas spent and the registries this key deployed."""
    spent, mine, deployed = 0, {}, dict(deployed)
    for at, batch in enumerate(batches(steps), 1):
        calls = [{"to": target(s, factory, deployed), "data": bytes.fromhex(s["data"][2:])} for s in batch]
        receipt = await sender.landed(calls, reserve(batch))
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
            announced = dict(zip((s["registry"] for s in batch), addresses, strict=True))
            deployed.update(announced)
            mine.update(announced)
        if at % REPORT_EVERY == 0:
            print(f"  {sender.address[:10]} tx {at}: {spent:,} gas", file=sys.stderr, flush=True)
    return spent, mine


def main() -> None:
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
    kinds = {k: sum(1 for s in steps if s["kind"] == k) for k in ("deploy", "record", "status", "leaves")}
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
        raise SystemExit("PRIVATE_KEYS is unset: pass the sending keys in the environment, not on the command line")
    try:
        spent = send(rpc=args.rpc, chain_id=args.chain_id, keys=keys, factory=args.factory, steps=steps)
    except RuntimeError as stopped:
        raise SystemExit(str(stopped)) from None
    print(f"sent {len(steps)} steps from {len(keys)} sender(s), {spent:,} gas")


if __name__ == "__main__":
    main()
