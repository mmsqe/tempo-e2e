"""Hardfork boundaries crossed on a running chain. Elsewhere forks start active, so xtask writes their state
into the alloc and the boundary installers never fire; which forks own such state is found by diffing allocs."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from web3 import AsyncWeb3, Web3

from .abi import ANCHORING, ANCHORING_ADDRESS, UPSTREAM_ZONE_OWNER, VALIDATOR_CONFIG_V2
from .anchoring import (
    FACTORY,
    IMPLEMENTATION,
    IMPLEMENTATION_SLOT,
    INIT_CODE,
    LAUNCH_RUNTIME_HASH,
    PROXY_CODE,
    RUNTIME_CODE,
    Page,
    deploy_relay,
    emitted,
    genesis_with_anchoring,
    seed_fixture,
)
from .network import (
    FAUCET_PRIVATE_KEY,
    TempoNode,
    dev_node,
    free_port,
    generate_dev_genesis,
    resolve_tempo_bin,
    xtask_forks,
)
from .utils import (
    FAR_FUTURE,
    STATE_WRITE_GAS,
    call_revert,
    latest_timestamp,
    rpc,
    send_call,
    send_calls,
    wait_for_block,
)

pytestmark = [pytest.mark.tempo, pytest.mark.slow]

# Launch-to-fork gap: startup plus some pre-fork blocks; blocks land every 50ms, so generous is cheap.
ACTIVATION_LEAD = 30

MMR_FORK = "t10_time"  # once put a precompile, which answers before code, at 0x…0a00
OWNER_FORK = "t12_time"
DEPLOY_GAS = 25_000_000  # 20.7 KB of code at 1,000 gas a byte, and the creation around it
ADMIN = Account.from_key(FAUCET_PRIVATE_KEY)  # the generated genesis's validator admin


def _read_alloc(genesis: Path) -> dict[str, dict]:
    return json.loads(genesis.read_text())["alloc"]


def _generate_alloc(output_dir: Path, fork_times: dict[str, int] | None = None) -> dict[str, dict]:
    return _read_alloc(generate_dev_genesis(output_dir, fork_times=fork_times))


def _schedule(fork: str, activation: int) -> dict[str, int]:
    """Activate ``fork`` and every later fork at ``activation``, leaving earlier ones at genesis; later
    forks come along because a schedule has to stay ordered."""
    order = xtask_forks()
    return {f: activation for f in order[order.index(fork) :]}


@functools.lru_cache(maxsize=1)
def boundary_forks() -> tuple[str, ...]:
    """Forks whose state xtask writes into the alloc only when active at genesis, each scheduled alone to
    isolate its footprint. Empty without xtask, which collects as a skip rather than an error."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            head = set(_generate_alloc(Path(tmp) / "head", dict.fromkeys(xtask_forks(), 0)))
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
    """Code, declared ``slots`` and dispatch at ``address``. Dispatch is probed with an unknown selector: a
    marker whose spec gate never opened answers unlike a live precompile, which state alone cannot show."""
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
    """The hardfork the node reports as active (``tempo_forkSchedule``), in xtask's name: ``T1A`` -> ``t1a_time``."""
    resp = w3.provider.make_request("tempo_forkSchedule", [])
    if resp.get("error"):
        raise RuntimeError(f"tempo_forkSchedule failed: {resp['error']}")
    return f"{resp['result']['active'].lower()}_time"


def _assert_pre_fork(w3: Web3, activation: int, fork: str) -> None:
    """Guard that the boundary is still ahead, so the pre-fork assertions mean something."""
    ts = w3.eth.get_block("latest")["timestamp"]
    assert ts < activation, (
        f"chain reached {fork} (block timestamp {ts} >= {activation}) "
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
    """A chain with every fork active at genesis, T12 named outright since xtask leaves it off unless asked."""
    node = dev_node(tmp_path_factory.mktemp("head"), log_name="head.log", fork_times=dict.fromkeys(xtask_forks(), 0))
    alloc = _read_alloc(node.genesis)
    with _running(node) as w3:
        yield w3, alloc


@pytest.mark.parametrize("fork", boundary_forks())
def test_boundary_installs_the_genesis_state_it_skipped(fork, head_chain, tmp_path):
    """Crossing ``fork`` installs exactly the state a chain launched with it holds; otherwise a node that
    upgraded through the fork would differ from one synced from genesis."""
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
        _assert_pre_fork(w3, activation, fork)
        assert _active_fork(w3) == forks[forks.index(fork) - 1]
        for address in owed:
            assert _code(w3, address) == b"", f"{address} must hold no code before {fork}"

        _wait_past(w3, activation)

        # Post-fork: the executor installed it all on a chain already running without it.
        assert _active_fork(w3) == forks[-1]
        assert fingerprints(w3) == fingerprints(head_w3)


def test_crossing_t10_leaves_the_anchoring_contract_alone(tmp_path):
    """Its code, storage and answers come through the boundary unchanged."""
    activation = int(time.time()) + ACTIVATION_LEAD
    seed = seed_fixture()
    genesis = genesis_with_anchoring(tmp_path, storage=seed, fork_times=_schedule(MMR_FORK, activation))
    node = TempoNode(datadir=tmp_path / "node0", log_path=tmp_path / "t10.log", genesis=genesis, http_port=free_port())
    listing = ANCHORING.fns.registries(0, Page()).data

    def state(w3: Web3):
        answer = bytes(w3.eth.call({"to": ANCHORING_ADDRESS, "data": listing}))
        return _fingerprint(w3, ANCHORING_ADDRESS, map(hex, seed)), answer

    with _running(node) as w3:
        _assert_pre_fork(w3, activation, MMR_FORK)
        before = state(w3)
        assert before[0]["code"] == PROXY_CODE.hex()

        _wait_past(w3, activation)

        assert _active_fork(w3) == xtask_forks()[-1]
        assert state(w3) == before


async def _deploy(send, salt: int = 0) -> str:
    """Deploys the implementation through the factory under ``salt``; returns where it landed."""
    salt_word = salt.to_bytes(32, "big")
    await send(FACTORY, salt_word + INIT_CODE, gas_limit=DEPLOY_GAS)
    return to_checksum_address(keccak(b"\xff" + bytes.fromhex(FACTORY[2:]) + salt_word + keccak(INIT_CODE))[12:])


async def _added(send, name: str) -> list[tuple]:
    """The registries an ``addRegistry`` call reports, as ``(caller, id, name)``."""
    return emitted(await send(ANCHORING_ADDRESS, ANCHORING.fns.addRegistry(name, "", "").data), "AddRegistry")


async def _implementation(w3: AsyncWeb3) -> str:
    """What the proxy delegates to."""
    return to_checksum_address((await w3.eth.get_storage_at(ANCHORING_ADDRESS, IMPLEMENTATION_SLOT))[12:])


async def _wait_for_timestamp(w3: AsyncWeb3, timestamp: int, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while await latest_timestamp(w3) < timestamp:
        if time.time() > deadline:
            raise TimeoutError(f"no block reached timestamp {timestamp} within {timeout}s")
        await asyncio.sleep(0.5)


@contextlib.asynccontextmanager
async def _launched(base: Path, genesis: Path):
    """A node on ``genesis``, its async client, and a sender bound to ``ADMIN``."""
    node = TempoNode(datadir=base / "node0", log_path=base / "node.log", genesis=genesis, http_port=free_port())
    try:
        async with rpc(node.start().wait_for_rpc()) as w3:
            yield node, w3, functools.partial(send_call, w3, node.chain_id, ADMIN)
    finally:
        node.stop()


class TestCrossingT12:
    """A chain launched on the ownerless build gets the proxy once T12 is active and the
    implementation is deployed, whichever comes last, owned by the validator registry's owner."""

    @staticmethod
    def genesis(base: Path, activation: int) -> Path:
        return generate_dev_genesis(base / "xtask", fork_times=_schedule(OWNER_FORK, activation), anchoring=True)

    @staticmethod
    async def assert_behind_the_proxy(w3: AsyncWeb3, send) -> None:
        """The launch build's registries carry on through the proxy."""
        assert await _added(send, "after") == [(ADMIN.address, 2, "after")]
        assert bytes(await w3.eth.get_code(ANCHORING_ADDRESS)) == PROXY_CODE
        assert await _implementation(w3) == IMPLEMENTATION
        registry_owner = await VALIDATOR_CONFIG_V2.fns.owner().call(w3)
        assert to_checksum_address(registry_owner) == ADMIN.address
        assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address

    async def test_the_implementation_deployed_first_takes_over_at_t12(self, tmp_path):
        activation = int(time.time()) + 2 * ACTIVATION_LEAD  # startup, then two transactions before T12
        async with _launched(tmp_path, self.genesis(tmp_path, activation)) as (_, w3, send):
            assert await _added(send, "before") == [(ADMIN.address, 1, "before")]
            await _deploy(send)
            assert await latest_timestamp(w3) < activation, "deployed too late to test"
            await _wait_for_timestamp(w3, activation)
            await self.assert_behind_the_proxy(w3, send)

    async def test_t12_waits_for_the_implementation(self, tmp_path):
        activation = int(time.time()) + ACTIVATION_LEAD
        async with _launched(tmp_path, self.genesis(tmp_path, activation)) as (_, w3, send):
            await _wait_for_timestamp(w3, activation)
            assert keccak(await w3.eth.get_code(ANCHORING_ADDRESS)) == LAUNCH_RUNTIME_HASH
            assert await _added(send, "before") == [(ADMIN.address, 1, "before")]
            await _deploy(send)
            await self.assert_behind_the_proxy(w3, send)

    async def test_shadow_replay_explains_every_change(self, tmp_path):
        """`tempo shadow-replay` explains every difference T12 makes to blocks made before it, on a
        chain launched like the testnet: its zone factory still on upstream's key."""
        genesis = generate_dev_genesis(
            tmp_path / "xtask",
            fork_times={OWNER_FORK: FAR_FUTURE},
            anchoring=True,
            zone_owner=UPSTREAM_ZONE_OWNER,
        )
        async with _launched(tmp_path, genesis) as (node, w3, send):
            # The shadow arm puts the proxy in at the block after this, and the calls below run through it.
            await _deploy(send)
            data = ANCHORING.fns.addRegistry("replayed", "", "").data
            await send(ANCHORING_ADDRESS, data)
            relay = await deploy_relay(w3, node.chain_id, ADMIN)
            calls = [{"to": relay, "data": data}]
            refused = await send_calls(
                w3, chain_id=node.chain_id, private_key=ADMIN.key.hex(), calls=calls, gas_limit=STATE_WRITE_GAS
            )
            assert refused["status"] == 0, "the launch build refuses a contract"
            await wait_for_block(w3, refused["blockNumber"] + 20)  # until the blocks above persist

        replay = subprocess.run(
            [resolve_tempo_bin(), "shadow-replay", "--chain", str(genesis), "--datadir", str(node.datadir)]
            + ["--hardfork", "T12", "--fail-on-findings"],
            capture_output=True,
            text=True,
            timeout=600,
            env=os.environ | {"RUST_LOG": "shadow_replay=debug"},
        )
        out = replay.stdout + replay.stderr
        assert replay.returncode == 0, out[-4000:]
        for rule in ("t12.anchoring-upgrade", "t12.anchoring-calls", "t12.zone-factory-handover"):
            assert rule in out, f"{rule} explained nothing"


class TestLaunchingOnT12:
    """A chain with T12 at genesis, xtask's default, starts on the proxy, owned from genesis and
    pointing at an implementation deployed after launch."""

    async def test_a_call_does_nothing_until_the_implementation_is_deployed(self, tmp_path):
        async with _launched(tmp_path, generate_dev_genesis(tmp_path / "xtask", anchoring=True)) as (_, w3, send):
            assert bytes(await w3.eth.get_code(ANCHORING_ADDRESS)) == PROXY_CODE
            assert bytes(await w3.eth.get_code(IMPLEMENTATION)) == b""
            assert bytes(await w3.eth.call({"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.owner().data})) == b""
            assert await _added(send, "lost") == []

            assert await _deploy(send) == IMPLEMENTATION
            assert bytes(await w3.eth.get_code(IMPLEMENTATION)) == RUNTIME_CODE
            assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address
            assert await _added(send, "first") == [(ADMIN.address, 1, "first")], "the call before left nothing"

    async def test_the_owner_upgrades(self, tmp_path):
        async with _launched(tmp_path, generate_dev_genesis(tmp_path / "xtask", anchoring=True)) as (_, w3, send):
            await _deploy(send)
            assert await _added(send, "before") == [(ADMIN.address, 1, "before")]
            second = await _deploy(send, salt=1)

            upgrade = ANCHORING.fns.upgradeToAndCall(second, b"").data
            assert "sender not the owner" in await call_revert(w3, ANCHORING_ADDRESS, upgrade, sender=second)
            assert emitted(await send(ANCHORING_ADDRESS, upgrade), "Upgraded") == [(second,)]
            assert await _implementation(w3) == second
            assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address
            assert await _added(send, "after") == [(ADMIN.address, 2, "after")]
