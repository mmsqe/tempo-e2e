"""Hardfork boundaries crossed on a running chain, rather than baked into genesis.

Every other test launches with all forks active, so xtask writes their state into the
alloc and the executor's boundary installers never fire. Scheduling a fork past genesis
makes the node install that state itself — the shape of every real upgrade. Which forks
own such state is discovered by diffing generated allocs: T3 (SignatureVerifier),
T6 (ReceivePolicyGuard) and T10 (TIP-1091 zone state) today.
"""

from __future__ import annotations

import contextlib
import functools
import json
import tempfile
import time
from pathlib import Path

import pytest
from web3 import Web3

from .abi import ANCHORING, ANCHORING_ADDRESS
from .network import dev_node, generate_dev_genesis, xtask_forks

pytestmark = [pytest.mark.tempo, pytest.mark.slow]

# Timestamp no run will reach, for allocs that are inspected but never launched.
FAR_FUTURE = 4_000_000_000

# Gap between launching a node and the fork activating: enough for startup and still some
# pre-fork blocks to observe. Blocks land every 50ms, so a generous value costs only time.
ACTIVATION_LEAD = 30

# Hardcoded rather than read from the node, so moving the fork has to be a decision.
ANCHORING_FORK = "t10_time"


def _fork_label(name: str) -> str:
    """The name ``tempo_forkSchedule`` reports: ``t1a_time`` -> ``T1a``."""
    return f"T{name.removeprefix('t').removesuffix('_time')}"


def _read_alloc(genesis: Path) -> dict[str, dict]:
    return json.loads(genesis.read_text())["alloc"]


def _generate_alloc(output_dir: Path, fork_times: dict[str, int] | None = None) -> dict[str, dict]:
    return _read_alloc(generate_dev_genesis(output_dir, fork_times=fork_times))


def _schedule(fork: str, activation: int, *, later: int | None = None) -> dict[str, int]:
    """Activate ``fork`` at ``activation``, leaving earlier forks at genesis.

    Later forks come along at ``later`` — by default same moment, since a schedule has to stay
    ordered. Pass ``FAR_FUTURE`` to hold them out, so boundary names one fork rather than its neighbour too.
    """
    order = xtask_forks()
    return {fork: activation, **{f: activation if later is None else later for f in order[order.index(fork) + 1 :]}}


@functools.lru_cache(maxsize=1)
def boundary_forks() -> tuple[str, ...]:
    """Forks whose state xtask writes into the alloc only when they are active at genesis.

    Each fork is scheduled alone — an ordering no chain would run, but these allocs are
    only read, so it isolates one fork's footprint. Empty when xtask is unavailable,
    which collects as a skip rather than an error.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            head = set(_generate_alloc(Path(tmp) / "head"))
            return tuple(f for f in xtask_forks() if head - set(_generate_alloc(Path(tmp) / f, {f: FAR_FUTURE})))
    except (OSError, RuntimeError):
        return ()


@contextlib.contextmanager
def _running(node):
    """Run ``node`` for the block, yielding a Web3 client bound to its RPC."""
    try:
        node.start().wait_for_rpc()
        yield Web3(Web3.HTTPProvider(node.rpc_url))
    finally:
        node.stop()


def _code(w3: Web3, address: str) -> bytes:
    return bytes(w3.eth.get_code(Web3.to_checksum_address(address)))


def _fingerprint(w3: Web3, address: str, slots) -> dict[str, str]:
    """Everything a boundary install must reproduce at ``address``: code, storage, dispatch.

    ``slots`` are the storage keys the genesis alloc declares. Dispatch is probed with an
    unknown selector, since a marker that landed without its spec gate opening answers
    unlike a live precompile — something reading state cannot see. Compared across chains,
    so no precompile's ABI is baked in here.
    """
    to = Web3.to_checksum_address(address)
    try:
        answer = "returned:" + bytes(w3.eth.call({"to": to, "data": b"\0\0\0\0"})).hex()
    except Exception as e:  # noqa: BLE001 - the exception type is the signal
        answer = f"raised:{type(e).__name__}"
    return {
        "code": _code(w3, address).hex(),
        "answer": answer,
        **{slot: bytes(w3.eth.get_storage_at(to, int(slot, 16))).hex() for slot in slots},
    }


def _active_fork(w3: Web3) -> str:
    """The hardfork the node reports as active (``tempo_forkSchedule``)."""
    resp = w3.provider.make_request("tempo_forkSchedule", [])
    if resp.get("error"):
        raise RuntimeError(f"tempo_forkSchedule failed: {resp['error']}")
    return resp["result"]["active"]


def _assert_pre_fork(w3: Web3, activation: int, label: str) -> None:
    """Guard that the boundary is still ahead, so the pre-fork assertions mean something."""
    ts = w3.eth.get_block("latest")["timestamp"]
    assert ts < activation, (
        f"chain reached {label} (block timestamp {ts} >= {activation}) "
        f"before the pre-fork assertions could run; raise ACTIVATION_LEAD"
    )


def _wait_past(w3: Web3, activation: int, timeout: float = 120.0) -> None:
    """Wait for a block at or past ``activation``."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if w3.eth.get_block("latest")["timestamp"] >= activation:
            return
        time.sleep(0.5)
    raise TimeoutError(f"no block reached timestamp {activation} within {timeout}s")


@pytest.fixture(scope="module")
def head_chain(tmp_path_factory):
    """A chain launched with every fork active at genesis — the state a boundary must reproduce."""
    node = dev_node(tmp_path_factory.mktemp("head"), log_name="head.log")
    alloc = _read_alloc(node.genesis)
    with _running(node) as w3:
        yield w3, alloc


@pytest.mark.parametrize("fork", boundary_forks())
def test_boundary_installs_the_genesis_state_it_skipped(fork, head_chain, tmp_path):
    """Crossing ``fork`` installs exactly the state a chain launched with it already holds.

    Both paths build from the same constructors upstream, so the two chains must come out
    identical — otherwise a node that upgraded through the fork carries different state
    than one synced from genesis.
    """
    head_w3, head_alloc = head_chain
    forks = xtask_forks()
    activation = int(time.time()) + ACTIVATION_LEAD
    node = dev_node(tmp_path, log_name=f"{fork}.log", fork_times=_schedule(fork, activation))

    # Whatever the scheduled genesis omits is precisely what the boundary installers owe.
    owed = sorted(set(head_alloc) - set(_read_alloc(node.genesis)))
    assert owed, f"{fork} writes nothing into the alloc; boundary_forks() should not have yielded it"

    def fingerprints(client: Web3) -> dict[str, dict[str, str]]:
        return {a: _fingerprint(client, a, head_alloc[a].get("storage", {})) for a in owed}

    with _running(node) as w3:
        # Pre-fork: a live chain one fork short, holding none of the state.
        _assert_pre_fork(w3, activation, _fork_label(fork))
        assert _active_fork(w3) == _fork_label(forks[forks.index(fork) - 1])
        for address in owed:
            assert _code(w3, address) == b"", f"{address} must hold no code before {_fork_label(fork)}"

        _wait_past(w3, activation)

        # Post-fork: the executor installed it all on a chain already running without it.
        assert _active_fork(w3) == _fork_label(forks[-1])
        assert fingerprints(w3) == fingerprints(head_w3)


def test_anchoring_precompile_installs_at_its_boundary(tmp_path):
    """Crossing its fork turns the anchoring address from an empty account into a precompile.

    The test above misses this one: it finds forks by diffing allocs, and anchoring writes
    nothing there. Dispatch is probed as well as code, since a marker installed without its
    spec gate opening looks the same in state.
    """
    activation = int(time.time()) + ACTIVATION_LEAD
    node = dev_node(
        tmp_path, log_name="anchoring.log", fork_times=_schedule(ANCHORING_FORK, activation, later=FAR_FUTURE)
    )
    assert ANCHORING_ADDRESS.lower() not in {a.lower() for a in _read_alloc(node.genesis)}, (
        "anchoring must not be in the alloc; it is installed at the boundary"
    )
    # `latest` on a never-anchored key: a zero word from a precompile, nothing from an account.
    probe = {"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.latest(ANCHORING_ADDRESS, b"\x00" * 32).data}

    forks = xtask_forks()
    with _running(node) as w3:
        _assert_pre_fork(w3, activation, _fork_label(ANCHORING_FORK))
        # Names the fork the absence holds at, rather than leaving it to the schedule.
        assert _active_fork(w3) == _fork_label(forks[forks.index(ANCHORING_FORK) - 1])
        assert _code(w3, ANCHORING_ADDRESS) == b"", f"no code before {_fork_label(ANCHORING_FORK)}"
        assert bytes(w3.eth.call(probe)) == b"", "an empty account answers nothing"

        _wait_past(w3, activation)

        assert _code(w3, ANCHORING_ADDRESS) == b"\xef", "the marker is installed at the boundary"
        assert bytes(w3.eth.call(probe)) == b"\x00" * 32, "and dispatch answers as a live precompile"
