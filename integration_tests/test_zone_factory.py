"""The zone factory (TIP-1091). Upstream installs it under its own key; this fork gives it to the
validator registry's owner, at genesis or at T12."""

import functools
import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from eth_account import Account
from eth_utils import to_checksum_address
from tempo.constants import PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS
from web3 import AsyncWeb3, Web3

from .abi import (
    UPSTREAM_ZONE_OWNER,
    ZONE_FACTORY,
    ZONE_FACTORY_ADDRESS,
    ZONE_MESSENGER_ADDRESS,
    ZONE_PORTAL,
    ZONE_PORTAL_IMPLEMENTATION,
    ZONE_VERIFIER_ADDRESS,
)
from .abi import VALIDATOR_CONFIG_V2 as V2
from .anchoring import emitted
from .network import FAUCET_PRIVATE_KEY, TempoNode, free_port, generate_dev_genesis, xtask_forks
from .utils import call_revert, create_token, new_account, send_call, wait_for_block

pytestmark = [pytest.mark.tempo]

ADMIN = Account.from_key(FAUCET_PRIVATE_KEY)  # the generated genesis's validator admin
NAMED = to_checksum_address("0x" + "b0" * 20)  # a --zone-owner
T12_LATER = {"t12_time": 4_000_000_000}
ZONE_GAS = 25_000_000  # creation charges 15M before it starts
ZONES_BEFORE = 3  # nextZoneId the testnet-like chain launches with, for the fork to keep
ACTIVATION_LEAD = 30  # seconds from launch to T12: node startup, then pre-fork blocks


def genesis_owner(genesis: Path) -> str:
    factory = json.loads(genesis.read_text())["alloc"][ZONE_FACTORY_ADDRESS.lower()]
    return to_checksum_address(f"0x{int(factory['storage'][f'0x{0:064x}'], 16) >> 32:040x}")


@contextmanager
def past_t12(base: Path, *, zone_owner: str = UPSTREAM_ZONE_OWNER, next_zone_id: int = 1, **options):
    """A node launched like the testnet, its factory on ``zone_owner`` with ``next_zone_id`` before
    T12, yielded once T12 is live."""
    activation = int(time.time()) + ACTIVATION_LEAD
    forks = xtask_forks()
    schedule = dict.fromkeys(forks[forks.index("t12_time") :], activation)
    genesis = generate_dev_genesis(base, fork_times=schedule, zone_owner=zone_owner, **options)
    launch = json.loads(genesis.read_text())
    storage, slot = launch["alloc"][ZONE_FACTORY_ADDRESS.lower()]["storage"], f"0x{0:064x}"
    storage[slot] = f"0x{int(storage[slot], 16) >> 32 << 32 | next_zone_id:064x}"
    genesis.write_text(json.dumps(launch))
    node = TempoNode(datadir=base / "node0", log_path=base / "node.log", genesis=genesis, http_port=free_port())
    try:
        node.start().wait_for_rpc()
        client = Web3(Web3.HTTPProvider(node.rpc_url))
        deadline = time.time() + ACTIVATION_LEAD + 90
        while client.eth.get_block("latest")["timestamp"] < activation:
            assert time.time() < deadline, f"no block reached T12 at {activation}"
            time.sleep(0.5)
        yield node
    finally:
        node.stop()


async def view(w3, fn, at: str = ZONE_FACTORY_ADDRESS, block: int | str = "latest"):
    """``fn`` called on ``at``, an address answer checksummed."""
    answer = await fn.call(w3, to=at, block_identifier=block)
    return to_checksum_address(answer) if isinstance(answer, str) else answer


def open_zone(token: str, admin: str, sequencer: str) -> tuple:
    """The least a zone takes: no access or gateway mode, one sequencer, no RPC URL."""
    return (token, False, False, [], [], admin, [sequencer], 1, "")


async def new_zone(w3, chain_id: int, name: str) -> tuple[int, str, str, str]:
    """``(zone id, portal, token, sequencer)`` of an open zone the owner creates on a new token."""
    token = await create_token(w3, chain_id=chain_id, admin=ADMIN, name=name, salt=name.encode().rjust(32, b"\0"))
    sequencer = new_account().address
    create = ZONE_FACTORY.fns.createZone(open_zone(token, ADMIN.address, sequencer))
    receipt = await send_call(w3, chain_id, ADMIN, ZONE_FACTORY_ADDRESS, create.data, gas_limit=ZONE_GAS)
    [(zone_id, portal, *_)] = emitted(receipt, "ZoneCreated", ZONE_FACTORY, ZONE_FACTORY_ADDRESS)
    return zone_id, portal, token, sequencer


class TestGenesis:
    """What the generator gives the factory, before any node runs."""

    @pytest.mark.parametrize(
        "options, owner",
        [
            pytest.param({}, ADMIN.address, id="default"),
            pytest.param({"fork_times": T12_LATER}, ADMIN.address, id="t12-later"),
            pytest.param({"anchoring": True, "anchoring_owner": "0x" + "a0" * 20}, ADMIN.address, id="anchoring-owner"),
            pytest.param({"zone_owner": NAMED}, NAMED, id="zone-owner"),
            pytest.param({"zone_owner": "0x" + "00" * 20}, "0x" + "00" * 20, id="nobody"),
        ],
    )
    def test_the_flags_pick_its_owner(self, tmp_path, options, owner):
        assert genesis_owner(generate_dev_genesis(tmp_path, **options)) == owner


@pytest.mark.slow
async def test_t12_leaves_a_named_owner_alone(tmp_path):
    """Only a factory still on upstream's key moves: one launched under any other owner stays."""
    with past_t12(tmp_path, zone_owner=NAMED) as node:
        w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(node.rpc_url))
        try:
            assert await view(w3, ZONE_FACTORY.fns.owner()) == NAMED
            schedule = (await w3.provider.make_request("tempo_forkSchedule", []))["result"]["schedule"]
            assert next(fork for fork in schedule if fork["name"] == "T12")["active"], "the fork did run here"
        finally:
            await w3.provider.disconnect()


@pytest.fixture(scope="module")
def tempo(request, tmp_path_factory):
    """The chain ``TestOwner`` and ``TestPortal`` share, launched like the testnet: zones made under
    upstream's key, T12 still to come, and no anchoring contract, which the hand-over never needed."""
    if request.config.getoption("--tempo-rpc"):
        pytest.skip("needs a chain this module launches")
    base = tmp_path_factory.mktemp("zones")
    with past_t12(base, next_zone_id=ZONES_BEFORE) as node:
        yield node


@pytest.mark.slow
class TestOwner:
    """The factory once T12 has moved it: the registry's owner creates zones and hands it on."""

    async def test_t12_moved_it_from_upstream_to_the_registry_owner(self, w3):
        assert await view(w3, ZONE_FACTORY.fns.owner(), block=0) == UPSTREAM_ZONE_OWNER
        registry = await view(w3, V2.fns.owner(), VALIDATOR_CONFIG_V2_ADDRESS)
        assert await view(w3, ZONE_FACTORY.fns.owner()) == registry == ADMIN.address

    @pytest.mark.parametrize("caller", [new_account().address, UPSTREAM_ZONE_OWNER], ids=["stranger", "upstream"])
    async def test_no_one_else_creates_a_zone_or_hands_it_on(self, w3, caller):
        for call in (
            ZONE_FACTORY.fns.createZone(open_zone(PATH_USD, caller, caller)),
            ZONE_FACTORY.fns.transferOwnership(caller),
        ):
            assert "NotOwner" in await call_revert(w3, ZONE_FACTORY_ADDRESS, call.data, sender=caller)

    async def test_the_owner_creates_the_next_zone_after_those_from_before(self, w3, chain_id):
        zone_id, portal, token, sequencer = await new_zone(w3, chain_id, "ZONE")

        assert zone_id >= ZONES_BEFORE
        assert await view(w3, ZONE_FACTORY.fns.nextZoneId()) == zone_id + 1
        assert await view(w3, ZONE_FACTORY.fns.isZonePortal(portal))
        info = await view(w3, ZONE_FACTORY.fns.zones(zone_id))
        assert (info[0], to_checksum_address(info[1]), to_checksum_address(info[4])) == (zone_id, portal, ADMIN.address)
        assert [to_checksum_address(s) for s in info[5]] == [sequencer]

    async def test_the_owner_hands_it_on_and_back(self, w3, chain_id, funded_account):
        """The fork never takes it back: the new owner still holds it blocks later."""

        def hand_to(account):
            return ZONE_FACTORY.fns.transferOwnership(account.address).data

        handed = functools.partial(
            emitted, event="OwnershipTransferred", contract=ZONE_FACTORY, address=ZONE_FACTORY_ADDRESS
        )

        receipt = await send_call(w3, chain_id, ADMIN, ZONE_FACTORY_ADDRESS, hand_to(funded_account))
        assert handed(receipt) == [(ADMIN.address, funded_account.address)]
        assert "NotOwner" in await call_revert(w3, ZONE_FACTORY_ADDRESS, hand_to(ADMIN), sender=ADMIN.address)
        await wait_for_block(w3, receipt["blockNumber"] + 2)
        assert await view(w3, ZONE_FACTORY.fns.owner()) == funded_account.address

        receipt = await send_call(w3, chain_id, funded_account, ZONE_FACTORY_ADDRESS, hand_to(ADMIN))
        assert handed(receipt) == [(funded_account.address, ADMIN.address)]


@pytest.mark.slow
class TestPortal:
    """The implementation behind every portal: T10's at genesis, T13's past the fork."""

    async def test_a_new_portal_runs_the_implementation(self, w3, chain_id):
        """It answers from the portal's storage, which ``createZone`` wrote; it keeps none itself."""
        zone_id, portal, token, sequencer = await new_zone(w3, chain_id, "PORTAL")

        delegate = "363d3d373d3d3d363d73" + ZONE_PORTAL_IMPLEMENTATION[2:] + "5af43d82803e903d91602b57fd5bf3"
        assert bytes(await w3.eth.get_code(portal)) == bytes.fromhex(delegate)
        for fn, answer in [
            (ZONE_PORTAL.fns.zoneId(), zone_id),
            (ZONE_PORTAL.fns.admin(), ADMIN.address),
            (ZONE_PORTAL.fns.messenger(), ZONE_MESSENGER_ADDRESS),
            (ZONE_PORTAL.fns.verifier(), ZONE_VERIFIER_ADDRESS),
            (ZONE_PORTAL.fns.leader(), sequencer),
            (ZONE_PORTAL.fns.sequencerThreshold(), 1),
            (ZONE_PORTAL.fns.isSequencer(sequencer), True),
            (ZONE_PORTAL.fns.isTokenEnabled(token), True),
        ]:
            assert await view(w3, fn, portal) == answer, fn
        assert await view(w3, ZONE_PORTAL.fns.zoneId(), ZONE_PORTAL_IMPLEMENTATION) == 0

    async def test_t13_replaced_the_implementation(self, w3, tempo, tmp_path):
        def implementation(genesis: Path) -> bytes:
            account = json.loads(genesis.read_text())["alloc"][ZONE_PORTAL_IMPLEMENTATION.lower()]
            return bytes.fromhex(account["code"].removeprefix("0x"))

        launched = implementation(tempo.genesis)
        t13 = implementation(generate_dev_genesis(tmp_path, fork_times={"t13_time": 0}))
        assert launched != t13
        assert bytes(await w3.eth.get_code(ZONE_PORTAL_IMPLEMENTATION, block_identifier=0)) == launched
        assert bytes(await w3.eth.get_code(ZONE_PORTAL_IMPLEMENTATION)) == t13
