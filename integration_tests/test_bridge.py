"""NVNM over LayerZero end to end: a bond bridged in from Ethereum stands its validator, a slash on
the L1 seizes the bond's NVNM on Ethereum, and an ordinary lock becomes stake and goes home."""

import asyncio
import os

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .abi import LZ_MINT_GATEWAY, NVNM_LOCKBOX, STAKING, TIP20_ROLES
from .anvil import DEPLOYER_KEY, MILLION, RELAYER_KEY
from .bridge import DEPLOYER, SEIZE_SINK, Bridge, deploy, eth_send, give_nvnm, lock, relay
from .staking import DAY
from .staking import deploy as deploy_staking
from .utils import ISSUER_ROLE, MAX_UINT, create_token, cs, fund, new_account, until, wait_for_timestamp

pytestmark = pytest.mark.requires("tempo-native")


AROUND = "around a staking"


@pytest.fixture
async def bridge(request, w3, eth):
    """Both gateways deployed and relayed both ways. An indirect param of True puts a TIP-20
    behind the L1 token; `AROUND` builds the bridge around a staking that already stands over
    one, as a live network takes it."""
    # Ethereum gas comes from anvil's genesis; the L1 charges pathUSD, so these pay from the faucet.
    for key in (DEPLOYER_KEY, RELAYER_KEY):
        await fund(w3, Account.from_key(key).address)
    mode = getattr(request, "param", False)
    if mode == AROUND:
        chain_id, salt = await w3.eth.chain_id, os.urandom(32)
        token = await create_token(w3, chain_id=chain_id, admin=DEPLOYER, name="NVNM", currency="NVNM", salt=salt)
        staking = await deploy_staking(w3, chain_id, DEPLOYER, reward_token=PATH_USD, stake_token=token)
        b = await deploy(eth, w3, staking=staking)
        # What the bridge cannot give itself: the token's admin and the staking's owner admit it.
        await staking.send(DEPLOYER, TIP20_ROLES.fns.grantRole(ISSUER_ROLE, b.token), to=token)
        await staking.send(DEPLOYER, STAKING.fns.setBondGateway(b.mint_gateway))
    else:
        b = await deploy(eth, w3, tip20=mode)
    # The slasher and the owner are one account here; it pays LayerZero for its slashes.
    await b.staking.send(DEPLOYER, ERC20.fns.approve(b.mint_gateway, MAX_UINT), to=PATH_USD)
    relays = [asyncio.create_task(relay(b.eth, b.l1)), asyncio.create_task(relay(b.l1, b.eth))]
    try:
        yield b
    finally:
        for r in relays:
            r.cancel()
        await asyncio.gather(*relays, return_exceptions=True)


EACH_BRIDGE = pytest.mark.parametrize(
    "bridge", [False, True, AROUND], ids=["bridged-nvnm", "tip20", "around-a-staking"], indirect=True
)


async def account(b: Bridge, nvnm: int = MILLION):
    """A fresh key on both chains, as a validator's is: ETH and `nvnm` NVNM on Ethereum, pathUSD on
    the L1, and the L1 gateway allowed to charge it LayerZero's fee there."""
    a = new_account()
    await eth_send(b.eth.w3, DEPLOYER_KEY, to=a.address, value=10**18)
    await give_nvnm(b, a.address, nvnm)
    await fund(b.l1.w3, a.address)
    await b.staking.send(a, ERC20.fns.approve(b.mint_gateway, MAX_UINT), to=PATH_USD)
    return a


async def candidates(b: Bridge) -> list[str]:
    return [cs(v) for v in await b.staking.call(STAKING.fns.candidates())]


@EACH_BRIDGE
class TestBond:
    async def test_a_slash_on_the_l1_seizes_the_bond_on_ethereum(self, bridge):
        b, staking, eth = bridge, bridge.staking, bridge.eth.w3
        bond = b.units(MILLION)
        await staking.setup_election(DEPLOYER, [], seats=1, unbonding=2)  # slashing opens with it
        await staking.send(DEPLOYER, STAKING.fns.setCandidacyBond(bond))
        await staking.send(DEPLOYER, STAKING.fns.setSlasher(DEPLOYER.address))
        v = await account(b)
        l1_bond, eth_bond = STAKING.fns.bondOf(v.address), NVNM_LOCKBOX.fns.bondOf(v.address)
        sunk, home = ERC20.fns.balanceOf(SEIZE_SINK), ERC20.fns.balanceOf(v.address)

        await lock(b, v.key.hex(), MILLION)
        await until("the bond to stand its validator", lambda: candidates(b), want=[v.address])
        assert await staking.call(l1_bond) == bond
        assert await staking.elected() == [v.address]
        assert await eth_bond.call(eth, to=b.lockbox) == MILLION

        before = await sunk.call(eth, to=b.nvnm)
        await staking.send(DEPLOYER, STAKING.fns.slash(v.address, 5_000))
        assert await staking.call(l1_bond) == bond // 2
        await until("the seizure on Ethereum", lambda: eth_bond.call(eth, to=b.lockbox), want=MILLION // 2)
        assert await sunk.call(eth, to=b.nvnm) - before == MILLION // 2
        assert await b.escrow() == await b.supply() == MILLION // 2

        # The rest goes home: resign, wait out the unbonding, withdraw.
        await staking.send(v, STAKING.fns.resignCandidate())
        _, release_at = await staking.call(STAKING.fns.pendingBondOf(v.address))
        await wait_for_timestamp(staking.w3, release_at)
        await staking.send(v, STAKING.fns.withdrawBond())
        await until("the bond home", lambda: home.call(eth, to=b.nvnm), want=MILLION // 2)
        assert await eth_bond.call(eth, to=b.lockbox) == 0
        assert await b.escrow() == await b.supply() == 0


class TestBondDelivery:
    async def test_a_bond_that_cannot_stand_is_still_recorded(self, bridge):
        """No delivery reverts for want of a seat: with candidacy closed the bond is held and stands
        once registration opens, and one arriving while the bond unbonds joins it."""
        b, staking = bridge, bridge.staking
        v = await account(b, nvnm=2 * MILLION)
        l1_bond = STAKING.fns.bondOf(v.address)

        await lock(b, v.key.hex(), MILLION)
        await until("the bond", lambda: staking.call(l1_bond), want=MILLION)
        assert await candidates(b) == []

        await staking.send(DEPLOYER, STAKING.fns.setUnbondingPeriod(DAY))
        await staking.send(DEPLOYER, STAKING.fns.setCandidacyBond(MILLION))
        await staking.send(v, STAKING.fns.registerCandidate())
        assert await candidates(b) == [v.address]

        await staking.send(v, STAKING.fns.resignCandidate())
        await lock(b, v.key.hex(), MILLION)
        await until("the second bond", lambda: staking.call(l1_bond), want=2 * MILLION)
        amount, release_at = await staking.call(STAKING.fns.pendingBondOf(v.address))
        assert amount == 2 * MILLION and release_at > 0, "it joins the unbonding bond"
        assert await candidates(b) == []
        assert await NVNM_LOCKBOX.fns.bondOf(v.address).call(b.eth.w3, to=b.lockbox) == 2 * MILLION


@EACH_BRIDGE
class TestRoundTrip:
    async def test_locked_nvnm_becomes_stake_that_elects_a_committee(self, bridge):
        b, staking = bridge, bridge.staking
        alice = await account(b)
        validator, idle = new_account().address, new_account().address

        await lock(b, alice.key.hex(), MILLION, validator)
        position = NVNM_LOCKBOX.fns.lockedOf(alice.address, validator)
        assert await position.call(b.eth.w3, to=b.lockbox) == MILLION
        held = ERC20.fns.balanceOf(alice.address)
        await until("the mint to alice", lambda: held.call(staking.w3, to=b.held), want=b.units(MILLION))

        # One seat between two candidates, so only weight decides it, and the only weight came
        # across the bridge.
        await staking.stake(alice, validator, b.units(MILLION))
        await staking.setup_election(DEPLOYER, [validator, idle], seats=1, unbonding=1)
        assert await staking.elected() == [cs(validator)]

        # And home: unstake, withdraw once the second has passed, then burn.
        await staking.send(alice, STAKING.fns.unstake(validator, b.units(MILLION)))
        _, release_at = await staking.call(STAKING.fns.pendingUnstakeOf(validator, alice.address))
        await wait_for_timestamp(staking.w3, release_at)
        await staking.send(alice, STAKING.fns.withdraw(validator))
        await staking.send(alice, ERC20.fns.approve(*b.burn_approval(MILLION)), to=b.held)
        withdraw = LZ_MINT_GATEWAY.fns.withdraw(MILLION, alice.address, validator)
        await staking.send(alice, withdraw, to=b.mint_gateway)
        assert await b.supply() == 0, "the L1 supply left with the burn"

        home = ERC20.fns.balanceOf(alice.address)
        await until("the release to alice", lambda: home.call(b.eth.w3, to=b.nvnm), want=MILLION)
        assert await b.escrow() == 0
