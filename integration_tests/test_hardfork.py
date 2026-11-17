"""Hardforks crossed on a running chain, and replayed over one by `tempo shadow-replay`."""

from __future__ import annotations

import contextlib
import functools
import os
import tempfile
import time
from pathlib import Path

import pytest
from eth_utils import keccak, to_checksum_address

from .abi import ACCOUNT_KEYCHAIN, ANCHORING, ANCHORING_ADDRESS, UPSTREAM_ZONE_OWNER, VALIDATOR_CONFIG_V2
from .anchoring import (
    IMPLEMENTATION,
    LAUNCH_RUNTIME_HASH,
    PROXY_CODE,
    RUNTIME_CODE,
    Page,
    add_registry,
    deploy_implementation,
    deploy_relay,
    genesis_with_anchoring,
    implementation,
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
    active_forks,
    call_revert,
    emitted,
    latest_timestamp,
    new_account,
    rpc,
    send_call,
    send_calls,
    wait_for_block,
    wait_for_timestamp,
)
from .utils import GENESIS_ADMIN as ADMIN

pytestmark = [pytest.mark.tempo, pytest.mark.slow]

MMR_FORK = "t10_time"  # once put a precompile, which answers before code, at 0x…0a00
OWNER_FORK = "t12_time"
SYSTEM_CALLS_FORK = "t13_time"
ADMIN_KEYS_FORK = "t14_time"

# Ethereum's beacon-root and request predeploys, whose system calls TIP-1119 drops at T13.
ETHEREUM_SYSTEM_CONTRACTS = (
    "0x000F3df6D732807Ef1319fB7B8bB8522d0Beac02",  # EIP-4788 beacon roots
    "0x00000961Ef480Eb55e80D19ad83579A64c007002",  # EIP-7002 withdrawal requests
    "0x0000BBdDc7CE488642fb579F8B00f3a590007251",  # EIP-7251 consolidation requests
)
CALL_COUNTER = "0x60005460010160005500"  # slot 0 += 1 on every call


def _fork_name(fork: str) -> str:
    """xtask's ``t1a_time`` as the node names it, ``T1A``."""
    return fork.removesuffix("_time").upper()


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

            def alloc(name: str, times: dict[str, int]) -> dict[str, dict]:
                return genesis_alloc(generate_dev_genesis(Path(tmp) / name, fork_times=times))

            head = alloc("head", dict.fromkeys(xtask_forks(), 0))
            return tuple(f for f in xtask_forks() if _owed(head, alloc(f, {f: FAR_FUTURE})))
    except (OSError, RuntimeError):
        return ()


def _node(base: Path, genesis: Path) -> TempoNode:
    return TempoNode(datadir=base / "node0", log_path=base / "node.log", genesis=genesis, http_port=free_port())


@contextlib.asynccontextmanager
async def _up(node: TempoNode):
    """Run ``node`` for the block, yielding its client and a sender bound to ``ADMIN``."""
    with running(node):
        async with rpc(node) as w3:
            yield w3, functools.partial(send_call, w3, node.chain_id, ADMIN)


async def _assert_before(w3, activation: int, fork: str) -> None:
    """Guard that the boundary is still ahead, so the pre-fork assertions mean something."""
    assert await latest_timestamp(w3) < activation, f"chain reached {fork} first; raise ACTIVATION_LEAD"


async def _fingerprint(w3, address: str, slots) -> dict[str, str]:
    """Code, declared ``slots`` and dispatch at ``address``. Dispatch is probed with an unknown selector: a
    marker whose spec gate never opened answers unlike a live precompile, which state alone cannot show."""
    to = to_checksum_address(address)
    try:
        answer = "returned:" + bytes(await w3.eth.call({"to": to, "data": b"\0\0\0\0"})).hex()
    except Exception as e:  # noqa: BLE001 - the exception type is the signal
        answer = f"raised:{type(e).__name__}"
    storage = {slot: bytes(await w3.eth.get_storage_at(to, int(slot, 16))).hex() for slot in slots}
    return {"code": bytes(await w3.eth.get_code(to)).hex(), "answer": answer, **storage}


@pytest.fixture(scope="module")
def head_chain(tmp_path_factory):
    """A running chain with every fork active at genesis, and its alloc; T12 named outright since xtask
    leaves it off unless asked."""
    node = dev_node(tmp_path_factory.mktemp("head"), log_name="head.log", fork_times=dict.fromkeys(xtask_forks(), 0))
    with running(node):
        yield node, genesis_alloc(node.genesis)


class TestForkBoundaries:
    """A chain crossing a fork. Elsewhere forks start active, so xtask writes their state into the alloc and
    the boundary installers never fire; here the chain must end up as one launched with the fork."""

    @pytest.mark.parametrize("fork", boundary_forks())
    async def test_a_boundary_installs_the_genesis_state_it_skipped(self, fork, head_chain, tmp_path):
        """Otherwise a node that upgraded through ``fork`` would differ from one synced from genesis."""
        head, head_alloc = head_chain
        activation = int(time.time()) + ACTIVATION_LEAD
        node = dev_node(tmp_path, log_name=f"{fork}.log", fork_times=fork_schedule(fork, activation))

        # Whatever the scheduled genesis omits or holds older code for is precisely what the boundary owes.
        scheduled = genesis_alloc(node.genesis)
        owed = _owed(head_alloc, scheduled)
        assert owed, f"{fork} writes nothing into the alloc; boundary_forks() should not have yielded it"

        async def fingerprints(w3) -> dict[str, dict[str, str]]:
            return {a: await _fingerprint(w3, a, head_alloc[a].get("storage", {})) for a in owed}

        async with _up(node) as (w3, _), rpc(head) as head_w3:
            # Pre-fork: a live chain one fork short, on the scheduled genesis's code.
            await _assert_before(w3, activation, fork)
            assert _fork_name(fork) not in await active_forks(w3)
            for address in owed:
                launch = bytes.fromhex(scheduled.get(address, {}).get("code", "0x")[2:])
                assert bytes(await w3.eth.get_code(to_checksum_address(address))) == launch, address

            await wait_for_timestamp(w3, activation)

            # Post-fork: the executor installed it all on a chain already running without it.
            assert {_fork_name(f) for f in xtask_forks()} <= await active_forks(w3)
            assert await fingerprints(w3) == await fingerprints(head_w3)

    async def test_crossing_t10_leaves_the_anchoring_contract_alone(self, tmp_path):
        """Its code, storage and answers come through the boundary unchanged."""
        activation = int(time.time()) + ACTIVATION_LEAD
        seed = seed_fixture()
        genesis = genesis_with_anchoring(tmp_path, storage=seed, fork_times=fork_schedule(MMR_FORK, activation))
        listing = ANCHORING.fns.registries(0, Page()).data

        async def state(w3):
            answer = bytes(await w3.eth.call({"to": ANCHORING_ADDRESS, "data": listing}))
            return await _fingerprint(w3, ANCHORING_ADDRESS, map(hex, seed)), answer

        async with _up(_node(tmp_path, genesis)) as (w3, _):
            await _assert_before(w3, activation, MMR_FORK)
            before = await state(w3)
            assert before[0]["code"] == PROXY_CODE.hex()

            await wait_for_timestamp(w3, activation)

            assert _fork_name(MMR_FORK) in await active_forks(w3)
            assert await state(w3) == before

    async def test_crossing_t13_stops_the_ethereum_system_calls(self, tmp_path):
        """TIP-1119: every block before T13 calls the beacon-root and request contracts once and none from
        it on does, so a counter at each stops at the number of the last block before the fork."""
        activation = int(time.time()) + ACTIVATION_LEAD
        genesis = generate_dev_genesis(tmp_path / "xtask", fork_times=fork_schedule(SYSTEM_CALLS_FORK, activation))
        assert not {a.lower() for a in ETHEREUM_SYSTEM_CONTRACTS} & set(genesis_alloc(genesis))
        counters = {a: {"balance": "0x0", "code": CALL_COUNTER} for a in ETHEREUM_SYSTEM_CONTRACTS}
        node = patched_node(tmp_path, genesis, lambda spec: spec["alloc"].update(counters))

        async def calls(w3, block) -> list[int]:
            return [int.from_bytes(await w3.eth.get_storage_at(a, 0, block), "big") for a in ETHEREUM_SYSTEM_CONTRACTS]

        async with _up(node) as (w3, _):
            await _assert_before(w3, activation, SYSTEM_CALLS_FORK)
            early = await w3.eth.block_number
            assert await calls(w3, early) == [early] * len(ETHEREUM_SYSTEM_CONTRACTS), "one call a block before T13"

            await wait_for_timestamp(w3, activation)
            await wait_for_block(w3, await w3.eth.block_number + 10)

            [last] = set(await calls(w3, "latest"))
            before, after = [await w3.eth.get_block(n) for n in (last, last + 1)]
            assert before["timestamp"] < activation <= after["timestamp"]

    async def test_crossing_t14_demotes_admin_keys(self, tmp_path):
        """TIP-1137: a key stored as an admin before T14 stays a key from it on, and no longer an admin."""
        activation = int(time.time()) + ACTIVATION_LEAD
        genesis = generate_dev_genesis(tmp_path / "xtask", fork_times=fork_schedule(ADMIN_KEYS_FORK, activation))
        async with _up(_node(tmp_path, genesis)) as (w3, send):
            key = new_account().address
            await send(ACCOUNT_KEYCHAIN.fns.authorizeAdminKey(key, 0, bytes(32)))
            assert await ACCOUNT_KEYCHAIN.fns.isAdminKey(ADMIN.address, key).call(w3)
            await _assert_before(w3, activation, ADMIN_KEYS_FORK)

            await wait_for_timestamp(w3, activation)
            assert not await ACCOUNT_KEYCHAIN.fns.isAdminKey(ADMIN.address, key).call(w3)
            _, key_id, _, _, is_revoked = await ACCOUNT_KEYCHAIN.fns.getKey(ADMIN.address, key).call(w3)
            assert (to_checksum_address(key_id), is_revoked) == (key, False)


class TestCrossingT12:
    """A chain launched on the ownerless build gets the proxy once T12 is active and the
    implementation is deployed, whichever comes last, owned by the validator registry's owner."""

    @staticmethod
    def chain(base: Path, activation: int):
        genesis = generate_dev_genesis(base / "xtask", fork_times=fork_schedule(OWNER_FORK, activation), anchoring=True)
        return _up(_node(base, genesis))

    @staticmethod
    async def assert_behind_the_proxy(w3, send) -> None:
        """The launch build's registries carry on through the proxy."""
        assert await add_registry(send, "after") == [(ADMIN.address, 2, "after")]
        assert bytes(await w3.eth.get_code(ANCHORING_ADDRESS)) == PROXY_CODE
        assert await implementation(w3) == IMPLEMENTATION
        assert to_checksum_address(await VALIDATOR_CONFIG_V2.fns.owner().call(w3)) == ADMIN.address
        assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address

    async def test_the_implementation_deployed_first_takes_over_at_t12(self, tmp_path):
        activation = int(time.time()) + 2 * ACTIVATION_LEAD  # startup, then two transactions before T12
        async with self.chain(tmp_path, activation) as (w3, send):
            assert await add_registry(send, "before") == [(ADMIN.address, 1, "before")]
            await deploy_implementation(send)
            await _assert_before(w3, activation, OWNER_FORK)
            await wait_for_timestamp(w3, activation)
            await self.assert_behind_the_proxy(w3, send)

    async def test_t12_waits_for_the_implementation(self, tmp_path):
        activation = int(time.time()) + ACTIVATION_LEAD
        async with self.chain(tmp_path, activation) as (w3, send):
            await wait_for_timestamp(w3, activation)
            assert keccak(await w3.eth.get_code(ANCHORING_ADDRESS)) == LAUNCH_RUNTIME_HASH
            assert await add_registry(send, "before") == [(ADMIN.address, 1, "before")]
            await deploy_implementation(send)
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
        node = _node(tmp_path, genesis)
        async with _up(node) as (w3, send):
            # The shadow arm puts the proxy in at the block after this, and the calls below run through it.
            await deploy_implementation(send)
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
        async with _up(_node(tmp_path, generate_dev_genesis(tmp_path / "xtask", anchoring=True))) as (w3, send):
            assert bytes(await w3.eth.get_code(ANCHORING_ADDRESS)) == PROXY_CODE
            assert bytes(await w3.eth.get_code(IMPLEMENTATION)) == b""
            assert bytes(await w3.eth.call({"to": ANCHORING_ADDRESS, "data": ANCHORING.fns.owner().data})) == b""
            assert await add_registry(send, "lost") == []

            assert await deploy_implementation(send) == IMPLEMENTATION
            assert bytes(await w3.eth.get_code(IMPLEMENTATION)) == RUNTIME_CODE
            assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address
            assert await add_registry(send, "first") == [(ADMIN.address, 1, "first")], "the call before left nothing"

    async def test_the_owner_upgrades(self, tmp_path):
        async with _up(_node(tmp_path, generate_dev_genesis(tmp_path / "xtask", anchoring=True))) as (w3, send):
            await deploy_implementation(send)
            assert await add_registry(send, "before") == [(ADMIN.address, 1, "before")]
            second = await deploy_implementation(send, salt=1)

            upgrade = ANCHORING.fns.upgradeToAndCall(second, b"")
            assert "sender not the owner" in await call_revert(w3, upgrade, sender=second)
            assert emitted(await send(upgrade), ANCHORING, "Upgraded") == [(second,)]
            assert await implementation(w3) == second
            assert to_checksum_address(await ANCHORING.fns.owner().call(w3)) == ADMIN.address
            assert await add_registry(send, "after") == [(ADMIN.address, 2, "after")]
