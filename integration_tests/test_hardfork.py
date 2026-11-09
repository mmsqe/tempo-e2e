"""Hardfork boundaries crossed on a running chain. Elsewhere forks start active, so xtask writes their state
into the alloc and the boundary installers never fire; which forks own such state is found by diffing allocs."""

from __future__ import annotations

import contextlib
import functools
import os
import tempfile
import time
from pathlib import Path

import pytest
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
    genesis_with_anchoring,
    seed_fixture,
)
from .network import (
    ACTIVATION_LEAD,
    TempoNode,
    dev_node,
    fork_schedule,
    free_port,
    generate_dev_genesis,
    genesis_alloc,
    patched_node,
    resolve_tempo_bin,
    run_binary,
    running,
    xtask_forks,
)
from .utils import (
    FAR_FUTURE,
    STATE_WRITE_GAS,
    call_revert,
    emitted,
    latest_timestamp,
    rpc,
    send_call,
    send_calls,
    wait_for_block,
    wait_for_timestamp,
    wait_height,
)
from .utils import GENESIS_ADMIN as ADMIN

pytestmark = [pytest.mark.tempo, pytest.mark.slow]

MMR_FORK = "t10_time"  # once put a precompile, which answers before code, at 0x…0a00
OWNER_FORK = "t12_time"
SYSTEM_CALLS_FORK = "t13_time"
DEPLOY_GAS = 25_000_000  # 20.7 KB of code at 1,000 gas a byte, and the creation around it

# Ethereum's beacon-root and request predeploys, whose system calls TIP-1119 drops at T13.
ETHEREUM_SYSTEM_CONTRACTS = (
    "0x000F3df6D732807Ef1319fB7B8bB8522d0Beac02",  # EIP-4788 beacon roots
    "0x00000961Ef480Eb55e80D19ad83579A64c007002",  # EIP-7002 withdrawal requests
    "0x0000BBdDc7CE488642fb579F8B00f3a590007251",  # EIP-7251 consolidation requests
)
CALL_COUNTER = "0x60005460010160005500"  # slot 0 += 1 on every call


def _generate_alloc(output_dir: Path, fork_times: dict[str, int] | None = None) -> dict[str, dict]:
    return genesis_alloc(generate_dev_genesis(output_dir, fork_times=fork_times))


def _owed(head: dict[str, dict], scheduled: dict[str, dict]) -> list[str]:
    """The accounts a genesis scheduling a fork lacks, or holds other code at, against one launched on it."""
    return sorted(
        a for a, account in head.items() if a not in scheduled or account.get("code") != scheduled[a].get("code")
    )


@functools.lru_cache(maxsize=1)
def boundary_forks() -> tuple[str, ...]:
    """Forks whose state xtask writes into the alloc only when active at genesis, each scheduled alone to
    isolate its footprint. Empty without xtask, which collects as a skip rather than an error."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            head = _generate_alloc(Path(tmp) / "head", dict.fromkeys(xtask_forks(), 0))
            return tuple(f for f in xtask_forks() if _owed(head, _generate_alloc(Path(tmp) / f, {f: FAR_FUTURE})))
    except (OSError, RuntimeError):
        return ()


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


@pytest.fixture(scope="module")
def head_chain(tmp_path_factory):
    """A chain with every fork active at genesis, T12 named outright since xtask leaves it off unless asked."""
    node = dev_node(tmp_path_factory.mktemp("head"), log_name="head.log", fork_times=dict.fromkeys(xtask_forks(), 0))
    with running(node):
        yield Web3(Web3.HTTPProvider(node.rpc_url)), genesis_alloc(node.genesis)


@pytest.mark.parametrize("fork", boundary_forks())
def test_boundary_installs_the_genesis_state_it_skipped(fork, head_chain, tmp_path):
    """Crossing ``fork`` installs exactly the state a chain launched with it holds; otherwise a node that
    upgraded through the fork would differ from one synced from genesis."""
    head_w3, head_alloc = head_chain
    forks = xtask_forks()
    activation = int(time.time()) + ACTIVATION_LEAD
    node = dev_node(tmp_path, log_name=f"{fork}.log", fork_times=fork_schedule(fork, activation))

    # Whatever the scheduled genesis omits or holds older code for is precisely what the boundary owes.
    scheduled = genesis_alloc(node.genesis)
    owed = _owed(head_alloc, scheduled)
    assert owed, f"{fork} writes nothing into the alloc; boundary_forks() should not have yielded it"

    def fingerprints(client: Web3) -> dict[str, dict[str, str]]:
        return {a: _fingerprint(client, a, head_alloc[a].get("storage", {})) for a in owed}

    with running(node):
        w3 = Web3(Web3.HTTPProvider(node.rpc_url))
        # Pre-fork: a live chain one fork short, on the scheduled genesis's code.
        _assert_pre_fork(w3, activation, fork)
        assert _active_fork(w3) == forks[forks.index(fork) - 1]
        for address in owed:
            launch = bytes.fromhex(scheduled.get(address, {}).get("code", "0x")[2:])
            assert _code(w3, address) == launch, f"{address} must hold its launch code before {fork}"

        node.wait_for_rpc(timeout=120, want_timestamp=activation)

        # Post-fork: the executor installed it all on a chain already running without it.
        assert _active_fork(w3) == forks[-1]
        assert fingerprints(w3) == fingerprints(head_w3)


def test_crossing_t10_leaves_the_anchoring_contract_alone(tmp_path):
    """Its code, storage and answers come through the boundary unchanged."""
    activation = int(time.time()) + ACTIVATION_LEAD
    seed = seed_fixture()
    genesis = genesis_with_anchoring(tmp_path, storage=seed, fork_times=fork_schedule(MMR_FORK, activation))
    node = TempoNode(datadir=tmp_path / "node0", log_path=tmp_path / "t10.log", genesis=genesis, http_port=free_port())
    listing = ANCHORING.fns.registries(0, Page()).data

    def state(w3: Web3):
        answer = bytes(w3.eth.call({"to": ANCHORING_ADDRESS, "data": listing}))
        return _fingerprint(w3, ANCHORING_ADDRESS, map(hex, seed)), answer

    with running(node):
        w3 = Web3(Web3.HTTPProvider(node.rpc_url))
        _assert_pre_fork(w3, activation, MMR_FORK)
        before = state(w3)
        assert before[0]["code"] == PROXY_CODE.hex()

        node.wait_for_rpc(timeout=120, want_timestamp=activation)

        assert _active_fork(w3) == xtask_forks()[-1]
        assert state(w3) == before


def test_crossing_t13_stops_the_ethereum_system_calls(tmp_path):
    """TIP-1119: every block before T13 calls the beacon-root and request contracts once and none from
    it on does, so a counter at each stops at the number of the last block before the fork."""
    activation = int(time.time()) + ACTIVATION_LEAD
    genesis = generate_dev_genesis(tmp_path / "xtask", fork_times=fork_schedule(SYSTEM_CALLS_FORK, activation))
    assert not {a.lower() for a in ETHEREUM_SYSTEM_CONTRACTS} & set(genesis_alloc(genesis))
    counters = {a: {"balance": "0x0", "code": CALL_COUNTER} for a in ETHEREUM_SYSTEM_CONTRACTS}
    node = patched_node(tmp_path, genesis, lambda spec: spec["alloc"].update(counters))

    def calls(w3: Web3, block) -> list[int]:
        return [int.from_bytes(w3.eth.get_storage_at(a, 0, block), "big") for a in ETHEREUM_SYSTEM_CONTRACTS]

    with running(node):
        w3 = Web3(Web3.HTTPProvider(node.rpc_url))
        _assert_pre_fork(w3, activation, SYSTEM_CALLS_FORK)
        early = w3.eth.block_number
        assert calls(w3, early) == [early] * len(ETHEREUM_SYSTEM_CONTRACTS), "one call a block before T13"

        node.wait_for_rpc(timeout=120, want_timestamp=activation)
        wait_height(node.rpc_url, w3.eth.block_number + 10)

        [last] = set(calls(w3, "latest"))
        assert w3.eth.get_block(last)["timestamp"] < activation <= w3.eth.get_block(last + 1)["timestamp"]


async def _deploy(send, salt: int = 0) -> str:
    """Deploys the implementation through the factory under ``salt``; returns where it landed."""
    salt_word = salt.to_bytes(32, "big")
    await send(FACTORY, salt_word + INIT_CODE, gas_limit=DEPLOY_GAS)
    return to_checksum_address(keccak(b"\xff" + bytes.fromhex(FACTORY[2:]) + salt_word + keccak(INIT_CODE))[12:])


async def _added(send, name: str) -> list[tuple]:
    """The registries an ``addRegistry`` call reports, as ``(caller, id, name)``."""
    return emitted(await send(ANCHORING.fns.addRegistry(name, "", "")), ANCHORING, "AddRegistry")


async def _implementation(w3: AsyncWeb3) -> str:
    """What the proxy delegates to."""
    return to_checksum_address((await w3.eth.get_storage_at(ANCHORING_ADDRESS, IMPLEMENTATION_SLOT))[12:])


@contextlib.asynccontextmanager
async def _launched(base: Path, genesis: Path):
    """A node on ``genesis``, its async client, and a sender bound to ``ADMIN``."""
    node = TempoNode(datadir=base / "node0", log_path=base / "node.log", genesis=genesis, http_port=free_port())
    with running(node):
        async with rpc(node) as w3:
            yield node, w3, functools.partial(send_call, w3, node.chain_id, ADMIN)


class TestCrossingT12:
    """A chain launched on the ownerless build gets the proxy once T12 is active and the
    implementation is deployed, whichever comes last, owned by the validator registry's owner."""

    @staticmethod
    def genesis(base: Path, activation: int) -> Path:
        return generate_dev_genesis(base / "xtask", fork_times=fork_schedule(OWNER_FORK, activation), anchoring=True)

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
            await wait_for_timestamp(w3, activation)
            await self.assert_behind_the_proxy(w3, send)

    async def test_t12_waits_for_the_implementation(self, tmp_path):
        activation = int(time.time()) + ACTIVATION_LEAD
        async with _launched(tmp_path, self.genesis(tmp_path, activation)) as (_, w3, send):
            await wait_for_timestamp(w3, activation)
            assert keccak(await w3.eth.get_code(ANCHORING_ADDRESS)) == LAUNCH_RUNTIME_HASH
            assert await _added(send, "before") == [(ADMIN.address, 1, "before")]
            await _deploy(send)
            await self.assert_behind_the_proxy(w3, send)

    async def test_shadow_replay_explains_every_change(self, tmp_path):
        """`tempo shadow-replay` explains every difference T12 makes to blocks made before it, on a
        chain launched like the testnet: its zone factory still on upstream's key."""
        genesis = generate_dev_genesis(
            tmp_path / "xtask",
            fork_times=fork_schedule(OWNER_FORK, FAR_FUTURE),
            anchoring=True,
            zone_owner=UPSTREAM_ZONE_OWNER,
        )
        async with _launched(tmp_path, genesis) as (node, w3, send):
            # The shadow arm puts the proxy in at the block after this, and the calls below run through it.
            await _deploy(send)
            write = ANCHORING.fns.addRegistry("replayed", "", "")
            await send(write)
            relay = await deploy_relay(w3, node.chain_id, ADMIN)
            calls = [{"to": relay, "data": write.data}]
            refused = await send_calls(
                w3, chain_id=node.chain_id, private_key=ADMIN.key.hex(), calls=calls, gas_limit=STATE_WRITE_GAS
            )
            assert refused["status"] == 0, "the launch build refuses a contract"
            await wait_for_block(w3, refused["blockNumber"] + 20)  # until the blocks above persist

        replay = run_binary(
            resolve_tempo_bin(),
            *("shadow-replay", "--chain", str(genesis), "--datadir", str(node.datadir)),
            *("--hardfork", "T12", "--fail-on-findings"),
            timeout=600,
            env=os.environ | {"RUST_LOG": "shadow_replay=debug"},
        )
        out = replay.stdout + replay.stderr
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

            upgrade = ANCHORING.fns.upgradeToAndCall(second, b"")
            assert "sender not the owner" in await call_revert(w3, upgrade, sender=second)
            assert emitted(await send(upgrade), ANCHORING, "Upgraded") == [(second,)]
            assert await _implementation(w3) == second
            assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address
            assert await _added(send, "after") == [(ADMIN.address, 2, "after")]
