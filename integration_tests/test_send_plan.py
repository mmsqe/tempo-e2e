"""How a plan is dealt across senders. Pure functions, so no chain and no fixtures."""

from .send_plan import evenly, shares


def record(registry, checksum, version=1):
    return {"kind": "record", "registry": registry, "checksum": checksum, "version": version}


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
