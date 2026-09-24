"""Hyperlane between anvil and the node, carried by its own agents: its stock Warp Route, then our
lockbox and BridgedNVNM behind Hyperlane routers."""

import asyncio
import shutil
import subprocess

import pytest
from eth_abi.abi import encode
from eth_account import Account
from eth_contract.erc20 import ERC20

from . import hyperlane as hl
from .abi import HL_LOCK_ROUTER, HL_MINT_ROUTER, HL_WARP, MOCK_ERC20, NVNM_LOCKBOX
from .anvil import ALICE_KEY, DEPLOYER_KEY
from .bridge import eth_send
from .staking import MOCK_ERC20_BYTECODE, transact
from .utils import new_account, rejects, until

pytestmark = pytest.mark.requires("tempo-native")

MILLION = 1_000_000 * 10**18
# Private Hyperlane domains, clear of every public chain's.
HUB_DOMAIN, L1_DOMAIN = 900_001, 900_002
ALICE = Account.from_key(ALICE_KEY)
TIMEOUT = 300  # agents poll, sign and deliver on their own schedule
IDLE_SECONDS = 60  # longer than a carried message takes


async def give(eth, token: str, spender: str):
    """Alice gets a million of mock `token` on the hub, approved to `spender`."""
    await eth_send(eth, DEPLOYER_KEY, to=token, data=MOCK_ERC20.fns.mint(ALICE.address, MILLION).data)
    await eth_send(eth, ALICE_KEY, to=token, data=ERC20.fns.approve(spender, MILLION).data)


async def lock(eth, r: hl.NvnmRoute, validator: str):
    """Alice gets a million NVNM on the hub and locks it through `r`'s router."""
    await give(eth, r.nvnm, r.lock_router)
    await eth_send(eth, ALICE_KEY, to=r.lock_router, data=HL_LOCK_ROUTER.fns.lock(validator, MILLION).data)


@pytest.fixture
async def hyperlane(w3, eth, ethereum, chain_id, driver, tmp_path):
    """Hyperlane's core on both chains, each trusting the other's validator, and both validators
    running. A test starts the relayer once it knows which routes to subsidize."""
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        pytest.skip("the Hyperlane agents run in Docker, which is not available")
    if subprocess.run(["docker", "image", "inspect", hl.IMAGE], capture_output=True).returncode:
        if subprocess.run(["docker", "pull", hl.IMAGE], capture_output=True).returncode:
            pytest.skip(f"cannot pull {hl.IMAGE}")

    # Gas on the L1 is a TIP-20 from the faucet; anvil's dev accounts are funded at genesis.
    for key in (DEPLOYER_KEY, ALICE_KEY, hl.RELAYER_KEY, hl.VALIDATOR_KEYS["nvnml1"]):
        await driver.fund(w3, Account.from_key(key).address, 10**16)

    hub = hl.Side(
        "anvilhub",
        eth,
        domain=HUB_DOMAIN,
        chain_id=ethereum.chain_id,
        rpc_url=ethereum.rpc_url,
        key=DEPLOYER_KEY,
        tempo=False,
    )
    l1 = hl.Side(
        "nvnml1",
        w3,
        domain=L1_DOMAIN,
        chain_id=chain_id,
        rpc_url=w3.provider.endpoint_uri,
        key=DEPLOYER_KEY,
        tempo=True,
    )
    hub_core = await hl.deploy_core(hub, trusts=hl.VALIDATOR_KEYS["nvnml1"])
    l1_core = await hl.deploy_core(l1, trusts=hl.VALIDATOR_KEYS["anvilhub"])

    agents = hl.Agents(tmp_path / "hyperlane", [(hub, hub_core), (l1, l1_core)])
    try:
        agents.validator("anvilhub")
        agents.validator("nvnml1")
        yield hl.Hyperlane(hub, l1, hub_core, l1_core, agents)
    finally:
        agents.stop()


async def test_tempo_cannot_pay_gas_in_native_value(w3):
    """Why the operator subsidizes delivery: Hyperlane's gas payment is msg.value, and tempo refuses
    any value transfer, whatever eth_getBalance reports."""
    resp = await w3.provider.make_request(
        "eth_call", [{"from": ALICE.address, "to": new_account().address, "value": "0x1"}, "latest"]
    )
    assert "value transfer not allowed" in str(resp.get("error")), resp


class TestWarpRoute:
    """Hyperlane's own contracts, unchanged."""

    async def test_nvnm_crosses_and_comes_back(self, w3, eth, chain_id, hyperlane):
        hub, l1 = hyperlane.hub, hyperlane.l1
        nvnm = await hub.create(MOCK_ERC20_BYTECODE, encode(["string", "string"], ["NVNM", "NVNM"]))
        collateral, synthetic = await hl.deploy_warp_route(hyperlane, nvnm)
        hyperlane.agents.relayer(subsidizing=[collateral, synthetic])

        # Out: escrowed in the collateral router on the hub, minted as a synthetic on the L1.
        await give(eth, nvnm, collateral)
        out = HL_WARP.fns.transferRemote(l1.domain, hl.pad(ALICE.address), MILLION)
        await eth_send(eth, ALICE_KEY, to=collateral, data=out.data)
        synth = ERC20.fns.balanceOf(ALICE.address)
        await until("the synthetic on the L1", lambda: synth.call(w3, to=synthetic), want=MILLION, timeout=TIMEOUT)

        # Home: burned on the L1, released from the collateral router.
        back = HL_WARP.fns.transferRemote(hub.domain, hl.pad(ALICE.address), MILLION)
        await transact(w3, chain_id, ALICE, synthetic, back)
        held = ERC20.fns.balanceOf(ALICE.address)
        await until("NVNM back on the hub", lambda: held.call(eth, to=nvnm), want=MILLION, timeout=TIMEOUT)
        assert await ERC20.fns.balanceOf(collateral).call(eth, to=nvnm) == 0


class TestNvnmRoute:
    """Our lockbox and token, with Hyperlane routers where the attested adapters were."""

    async def test_positions_and_the_invariant_survive_the_swap(self, w3, eth, chain_id, hyperlane):
        r = await hl.deploy_nvnm_route(hyperlane)
        hyperlane.agents.relayer(subsidizing=[r.lock_router, r.mint_router])
        validator = new_account().address
        supply, escrow = ERC20.fns.totalSupply(), NVNM_LOCKBOX.fns.totalLocked()

        # Out: the router locks into alice's own position, and Hyperlane mints to her on the L1.
        await lock(eth, r, validator)
        position = NVNM_LOCKBOX.fns.lockedOf(ALICE.address, validator)
        assert await position.call(eth, to=r.lockbox) == MILLION, "the position is alice's, not the router's"

        minted = ERC20.fns.balanceOf(ALICE.address)
        await until("BridgedNVNM on the L1", lambda: minted.call(w3, to=r.token), want=MILLION, timeout=TIMEOUT)
        assert await escrow.call(eth, to=r.lockbox) == await supply.call(w3, to=r.token) == MILLION

        # Home: burned on the L1, released from alice's position by the lockbox.
        await transact(w3, chain_id, ALICE, r.token, ERC20.fns.approve(r.mint_router, MILLION))
        dust = HL_MINT_ROUTER.fns.withdraw(hl.MIN_WITHDRAWAL - 1, ALICE.address, validator)
        await rejects(w3, r.mint_router, dust, "BelowMinimum", sender=ALICE.address)
        withdraw = HL_MINT_ROUTER.fns.withdraw(MILLION, ALICE.address, validator)
        await transact(w3, chain_id, ALICE, r.mint_router, withdraw)
        held = ERC20.fns.balanceOf(ALICE.address)
        await until("NVNM released on the hub", lambda: held.call(eth, to=r.nvnm), want=MILLION, timeout=TIMEOUT)
        assert await escrow.call(eth, to=r.lockbox) == await supply.call(w3, to=r.token) == 0

    async def test_a_route_the_operator_does_not_subsidize_is_not_carried(self, eth, w3, hyperlane):
        """What bounds the subsidy: another route on the same Mailbox waits while ours arrives."""
        ours, theirs = await hl.deploy_nvnm_route(hyperlane), await hl.deploy_nvnm_route(hyperlane)
        hyperlane.agents.relayer(subsidizing=[ours.lock_router, ours.mint_router])
        for r in (ours, theirs):
            await lock(eth, r, new_account().address)

        minted = ERC20.fns.balanceOf(ALICE.address)
        await until("the subsidized mint", lambda: minted.call(w3, to=ours.token), want=MILLION, timeout=TIMEOUT)
        await asyncio.sleep(IDLE_SECONDS)
        assert await minted.call(w3, to=theirs.token) == 0, "the relayer carried a route it does not subsidize"
