"""Measured: what one call costs, for the tables the PRs quote and the constants
`send_plan` reserves by.

Skipped unless ``PERCALL`` is set. The corpus runs measure whole paths; these are the
individual calls those tables name, taken from receipts on a fresh registry so a first
version really is first and a later one really finds the tree warm.
"""

import json
import os

import pytest

from .abi import REGISTRY as REG
from .send_plan import BUDGET, GAS
from .utils import funded

pytestmark = pytest.mark.tempo
if not os.environ.get("PERCALL"):
    pytest.skip("PERCALL is unset", allow_module_level=True)


async def test_per_call(w3, factory, registry):
    """Each figure the PR tables quote, from one receipt each."""
    creator, out = registry.creator, {}
    out["deployRegistry"] = int(registry.deployment["gasUsed"])

    r = await registry.add_record(creator, "cs-1")
    out["addRecord, first version"] = int(r["gasUsed"])
    r = await registry.add_record(creator, "cs-1")
    out["addRecord, later version"] = int(r["gasUsed"])
    r = await registry.set_status(creator, "cs-1", 1, "Active")
    out["updateRecordStatus"] = int(r["gasUsed"])

    # A leaf straight through the registry's forwarding, which is what a migration sends.
    # Three leaves are in by now, so the next append carries 0b11 up and writes height 2,
    # a slot nothing has touched; the one after it writes height 0 again, which is warm.
    r = await registry.write(creator, REG.fns.appendLeaf(b"\x11" * 32, b""))
    out["appendLeaf, opening a height"] = int(r["gasUsed"])
    r = await registry.write(creator, REG.fns.appendLeaf(b"\x22" * 32, b""))
    out["appendLeaf, overwriting a height"] = int(r["gasUsed"])

    # A batch only aligns from a count that is a multiple of each chunk's size, so it goes
    # to a registry of its own: 1013 = 0b1111110101, eight chunks, descending from empty.
    fresh = await factory.deploy(await funded(w3))
    chunks = [(bytes([i + 1]) * 32, h) for i, h in enumerate((9, 8, 7, 6, 5, 4, 2, 0))]
    r = await fresh.write(fresh.creator, REG.fns.appendLeaves(chunks, b""))
    out["appendLeaves, 1,013 rows as 8 chunks"] = int(r["gasUsed"])
    r = await fresh.write(fresh.creator, REG.fns.appendLeaf(b"\x33" * 32, b""))
    out["appendLeaf after that batch, 8 peaks live"] = int(r["gasUsed"])

    print("PERCALL " + json.dumps(out))
    # The claim these tables are quoted for: a height's slot is paid for once, so the append
    # that opens one costs several times the append that lands on a height already there.
    assert out["appendLeaf, opening a height"] > 4 * out["appendLeaf, overwriting a height"]

    # A deploy is the one step `send_plan` prices flat, and the only one gas bounds
    # rather than the call cap: `BUDGET // GAS["deploy"]` is how many go in a
    # transaction. Grow past it and every deploy batch reverts, so this is checked
    # against the chain rather than left to drift. The rest of the reservation is
    # arithmetic over these figures, and `test_send_plan` covers it without a node.
    assert out["deployRegistry"] <= GAS["deploy"], (
        f"a deploy now costs {out['deployRegistry']:,}, over the {GAS['deploy']:,} "
        f"send_plan reserves — {BUDGET // GAS['deploy']} to a transaction would revert"
    )
