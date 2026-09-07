"""How a plan is dealt across senders. Pure functions, so no chain and no fixtures."""

from .send_plan import (
    BUDGET,
    BYTES_BUDGET,
    CALL_OVERHEAD,
    CALLDATA_GAS,
    FRESH_SLOT,
    GAS,
    GAS_CAP,
    GAS_HEADROOM,
    MAX_CALLS,
    RECORD_BYTE_GAS,
    RECORD_BYTES_AT,
    batched,
    cost,
    evenly,
    reserve,
    shares,
    size,
)


def record(registry, checksum, version=1):
    return {"kind": "record", "registry": registry, "checksum": checksum, "version": version}


def sized(kind, calldata_bytes, **fields):
    """A step carrying `calldata_bytes` of data, for what the batching weighs."""
    return {"kind": kind, "registry": "r", "data": "0x" + "00" * calldata_bytes} | fields


def test_records_fill_a_transaction_to_the_call_cap():
    """A first version is cheap enough that the gas budget would take eighty-odd, but the pool
    refuses a transaction past `MAX_AA_CALLS`, so the cap is what bounds the batch."""
    plan = [sized("record", 500, checksum=str(i), version=1) for i in range(300)]
    lots = list(batched(plan))
    assert BUDGET // GAS["first"] > MAX_CALLS, "gas would not be what binds"
    assert len(lots[0]) == MAX_CALLS
    assert all(len(lot) * GAS["first"] <= BUDGET for lot in lots)
    assert sum(len(lot) for lot in lots) == 300


def test_statuses_fill_a_transaction_to_the_call_cap():
    """A status is cheap in both gas and calldata, so neither budget is what stops the batch."""
    plan = [sized("status", 230, checksum=str(i), version=1, status="Active") for i in range(2000)]
    lots = list(batched(plan))
    assert BYTES_BUDGET // (230 + CALL_OVERHEAD) > MAX_CALLS, "calldata would not be what binds"
    assert len(lots[0]) == MAX_CALLS
    assert sum(len(lot) for lot in lots) == 2000


def test_deploys_are_bound_by_gas_below_the_cap():
    """The one step the budget still bounds: a deploy is dear enough that a transaction fills
    on gas well before it reaches the call cap, which is what keeps `BUDGET` load-bearing."""
    plan = [sized("deploy", 500, registry=str(i)) for i in range(60)]
    lots = list(batched(plan))
    assert len(lots[0]) == BUDGET // GAS["deploy"] < MAX_CALLS
    assert all(len(lot) * GAS["deploy"] <= BUDGET for lot in lots)
    assert sum(len(lot) for lot in lots) == 60


def status(registry, checksum, version=1):
    return {"kind": "status", "registry": registry, "checksum": checksum, "version": version}


def test_a_record_keeps_its_status():
    """`updateRecordStatus` reverts unless the record is already there, so the two may not
    be dealt to senders that run at the same time."""
    plan = [record("r", "a"), status("r", "a"), record("r", "b"), status("r", "b")]
    assert [[s["checksum"] for s in lot] for lot in evenly(plan, 2)] == [["a", "a"], ["b", "b"]]


def test_a_record_keeps_its_later_versions():
    """A version is numbered by the order it lands in, so one record's versions stay on one
    sender even though the planner emits another record between them."""
    plan = [record("r", "a"), record("r", "b"), record("r", "a", version=2)]
    assert [[(s["checksum"], s["version"]) for s in lot] for lot in evenly(plan, 2)] == [
        [("a", 1), ("a", 2)],
        [("b", 1)],
    ]


def test_records_of_one_registry_still_spread():
    """The grants are what make this safe: after them any sender may write any registry, and
    a corpus whose largest registry holds a tenth of it needs that to split at all."""
    plan = [record("r", str(i)) for i in range(8)]
    assert [len(lot) for lot in evenly(plan, 4)] == [2, 2, 2, 2]


def test_steps_without_a_checksum_stand_alone():
    """A root commits a whole registry in one call, so it depends on nothing and groups with
    nothing."""
    plan = [{"kind": "leaves", "registry": "q"}, {"kind": "leaves", "registry": "q"}]
    assert [len(lot) for lot in evenly(plan, 2)] == [1, 1]


def test_deploys_go_by_registry():
    """A deploy's sender becomes that registry's admin, so a registry's deploys may not be
    split; sorting by name means a resumed run deals them the same way."""
    plan = [{"kind": "deploy", "registry": r} for r in ("b", "a", "b")]
    assert [[s["registry"] for s in lot] for lot in shares(plan, 2)] == [["a"], ["b", "b"]]


def test_a_batch_reserves_what_it_plans_to_spend():
    """A block keeps `GAS_CAP` for non-payment transactions and fits them in by their limit,
    so a full batch of first versions reserves what it plans, its calldata and the peak slots
    it may open, not the cap: one batch at what it actually uses, about 9.1M, leaves room for
    another's reservation. A batch with a leaves step keeps the cap, since its cost is a guess."""
    firsts = [sized("record", 500, checksum=str(i), version=1) for i in range(MAX_CALLS)]
    planned = MAX_CALLS * (GAS["first"] + CALLDATA_GAS * (500 + CALL_OVERHEAD)) + 6 * FRESH_SLOT
    assert reserve(firsts) == int(planned * GAS_HEADROOM)
    assert 9_100_000 + reserve(firsts) <= GAS_CAP, "two to a block"
    deploys = [sized("deploy", 500, registry=str(i)) for i in range(4)]
    assert reserve(deploys) == int(4 * (GAS["deploy"] + CALLDATA_GAS * (500 + CALL_OVERHEAD)) * GAS_HEADROOM)
    leaves = [{"kind": "leaves", "registry": "q", "data": "0x" + "00" * 100}]
    assert reserve(leaves) == GAS_CAP


def test_a_fresh_registrys_first_batch_is_covered():
    """What sank the 8% reservation on a fresh node: sixteen records and their statuses, each
    a leaf, the first thirty-two leaves of a registry opening six peak heights at a fresh
    slot each, on top of what the per-step costs plan for."""
    mixed = []
    for i in range(16):
        mixed.append(sized("record", 600, checksum=str(i), version=1))
        mixed.append(sized("status", 230, checksum=str(i), version=1, status="Active"))
    used = 16 * 283_000 + 16 * 21_000 + 6 * FRESH_SLOT + 21_000  # measured, plus the intrinsic gas
    assert reserve(mixed) > used
    assert reserve(mixed) < 2 * used, "and not so much that one batch is a block"


def test_a_record_is_charged_for_what_it_stores():
    """`GAS["first"]` was measured over the replay's records. A rooted one carries the legacy
    envelope instead, half as long again, and reserving the flat figure for thirty-two of them
    ran out of gas — so the cost follows the bytes beyond the shape it was measured on."""
    plain = sized("record", RECORD_BYTES_AT, checksum="a", version=1)
    rooted = sized("record", 900, checksum="b", version=1)
    assert cost(plain) == GAS["first"]
    assert cost(rooted) == GAS["first"] + RECORD_BYTE_GAS * (900 - RECORD_BYTES_AT)
    # 13,802,166 is what a batch of thirty-two rooted records reserved before, and reverted at.
    assert reserve([sized("record", 900, checksum=str(i), version=1) for i in range(MAX_CALLS)]) > 13_802_166
    # A later version stores a word, not strings, so length does not move it.
    assert cost(sized("record", 900, checksum="c", version=2)) == GAS["later"]


def test_a_batch_over_many_trees_keeps_the_cap():
    """`GAS` averages a registry's records, where all but the first find the tree warm. The
    corpus's tail of small registries puts every call on a cold one: chunk 21 wanted 649k a
    call against 330k planned and reverted at 20,773,314. Those batches keep the cap."""
    one = [sized("record", 612, registry="r", checksum=str(i), version=1) for i in range(MAX_CALLS)]
    many = [sized("record", 612, registry=f"r{i}", checksum=str(i), version=1) for i in range(MAX_CALLS)]
    assert reserve(one) < GAS_CAP, "a batch within one tree is still priced"
    assert reserve(many) == GAS_CAP
    assert reserve(many) > 20_773_314


def test_fresh_slots_are_counted_per_registry():
    """Within `SAME_TREE`, the precompile still creates a slot the first time each tree
    reaches a height, so two registries open more than one counted over the batch."""
    pair = [sized("record", 612, registry=f"r{i % 2}", checksum=str(i), version=1) for i in range(MAX_CALLS)]
    one = [sized("record", 612, registry="r", checksum=str(i), version=1) for i in range(MAX_CALLS)]
    assert reserve(pair) > reserve(one)
    assert reserve(pair) == int(
        (
            sum(cost(s) for s in pair)
            + CALLDATA_GAS * sum(size(s) for s in pair)
            + FRESH_SLOT * 2 * (MAX_CALLS // 2).bit_length()
        )
        * GAS_HEADROOM
    )
