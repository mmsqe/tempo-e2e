"""The bridge end to end, unstubbed: NVNM locked on Ethereum becomes stake that wins a seat, and
what the shipped services do when one of them is missing, doubled, or lied to."""

import asyncio
from typing import NamedTuple

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import AsyncWeb3, Web3

from . import bridge as bridge_mod
from .abi import BRIDGED_NVNM, NVNM_BRIDGE_ADAPTER, NVNM_LOCKBOX, STAKING
from .anvil import ALICE_KEY, ATTESTOR_KEYS, DEPLOYER_KEY, RELAYER_KEY, SECOND_RELAYER_KEY
from .staking import deploy as deploy_staking
from .staking import transact
from .utils import new_account, until

pytestmark = pytest.mark.requires("tempo-native")

MILLION = 1_000_000 * 10**18
# Gas for the L1 side. The relayer spends the most: a mint per transfer, forever.
L1_GAS_FUNDING = 10**16
# Long enough for a relayer polling each second to take several passes at doing the wrong thing.
IDLE_SECONDS = 5
DEPLOYER, ALICE = Account.from_key(DEPLOYER_KEY), Account.from_key(ALICE_KEY)
cs = Web3.to_checksum_address


class Stack(NamedTuple):
    """Both ends of the bridge and its services, with the chain clients bound once."""

    bridge: bridge_mod.Bridge
    services: bridge_mod.Services
    w3: AsyncWeb3  # the tempo node
    eth: AsyncWeb3  # the anvil standing in for Ethereum

    async def supply(self) -> int:
        return await ERC20.fns.totalSupply().call(self.w3, to=self.bridge.token)

    async def escrow(self) -> int:
        return await NVNM_LOCKBOX.fns.totalLocked().call(self.eth, to=self.bridge.lockbox)

    async def minted_to(self, who: str) -> int:
        return await ERC20.fns.balanceOf(who).call(self.w3, to=self.bridge.token)

    async def monitor(self) -> tuple[int, str]:
        return await asyncio.to_thread(self.services.monitor)

    async def lock(self, validator: str, amount: int, *, key: str = ALICE_KEY, holder: str | None = None):
        """The account behind `key` gets `amount` NVNM and locks it toward `validator`, into its own
        position or, through `lockFor`, into `holder`'s."""
        b, payer = self.bridge, Account.from_key(key).address
        await bridge_mod.give_nvnm(self.eth, b, payer, amount)
        await bridge_mod.eth_send(self.eth, key, to=b.nvnm, data=ERC20.fns.approve(b.lockbox, amount).data)
        call = (
            NVNM_LOCKBOX.fns.lockFor(holder, validator, amount) if holder else NVNM_LOCKBOX.fns.lock(validator, amount)
        )
        await bridge_mod.eth_send(self.eth, key, to=b.lockbox, data=call.data)


@pytest.fixture
async def stack(w3, eth, ethereum, chain_id, driver, bridge_bin_dir, tmp_path):
    """Both ends deployed and wired, services ready but none started: which ones run is what most
    of these tests are about."""
    if missing := bridge_mod.missing_binary(bridge_bin_dir):
        pytest.skip(f"no bridge services built: {missing} (build them, or pass --bridge-bin-dir)")
    # Ethereum gas comes from anvil's genesis; the L1 charges a TIP-20, so these pay from the faucet.
    for key in (DEPLOYER_KEY, ALICE_KEY, RELAYER_KEY, SECOND_RELAYER_KEY):
        await driver.fund(w3, Account.from_key(key).address, L1_GAS_FUNDING)

    eth_from, l1_from = await eth.eth.block_number, await w3.eth.block_number
    bridge = await bridge_mod.deploy(eth, w3, chain_id, DEPLOYER)
    services = bridge_mod.Services(
        bridge_bin_dir,
        tmp_path / "bridge",
        bridge=bridge,
        eth_rpc=ethereum.rpc_url,
        l1_rpc=w3.provider.endpoint_uri,
        eth_from=eth_from,
        l1_from=l1_from,
    )
    try:
        yield Stack(bridge, services, w3, eth)
    finally:
        services.stop()


class TestRoundTrip:
    """NVNM across and back with every service running."""

    async def test_locked_nvnm_becomes_stake_that_elects_a_committee(self, w3, eth, chain_id, stack):
        bridge = stack.bridge
        stack.services.start()
        validator, idle = new_account().address, new_account().address

        await stack.lock(validator, MILLION)
        assert await NVNM_LOCKBOX.fns.lockedOf(ALICE.address, validator).call(eth, to=bridge.lockbox) == MILLION
        await until("the mint to alice", lambda: stack.minted_to(ALICE.address), want=MILLION)
        assert all(stack.services.signed(i) >= 1 for i in range(len(ATTESTOR_KEYS))), "every attestor signed"
        assert await stack.escrow() == await stack.supply() == MILLION

        # One seat between two candidates, so only weight decides it, and the only weight came
        # across the bridge.
        staking = await deploy_staking(w3, chain_id, DEPLOYER, reward_token=PATH_USD, stake_token=bridge.token)
        assert cs(staking.nvnm) == cs(bridge.token), "staking has to be over the bridged token"
        await staking.stake(ALICE, validator, MILLION)
        for candidate in (validator, idle):
            await staking.send(DEPLOYER, STAKING.fns.setCandidate(candidate, True))
        await staking.send(DEPLOYER, STAKING.fns.setCommitteeConfig(1, 1, 0))
        assert await staking.elected() == [cs(validator)]

        # And home: unstake (a zero unbonding period pays out inside `unstake`), then burn.
        await staking.send(ALICE, STAKING.fns.unstake(validator, MILLION))
        await staking.send(ALICE, ERC20.fns.approve(bridge.adapter, MILLION), to=bridge.token)
        withdraw = NVNM_BRIDGE_ADAPTER.fns.withdraw(MILLION, ALICE.address, validator)
        await staking.send(ALICE, withdraw, to=bridge.adapter)
        assert await stack.supply() == 0, "the L1 supply left with the burn"

        balance = ERC20.fns.balanceOf(ALICE.address)
        await until("the release to alice", lambda: balance.call(eth, to=bridge.nvnm), want=MILLION)
        assert await stack.escrow() == 0

    async def test_a_lock_made_for_someone_mints_to_them(self, stack):
        """What a third-party bridge's router would do: the mint follows the holder, not the payer."""
        stack.services.start()
        await stack.lock(new_account().address, MILLION, key=DEPLOYER_KEY, holder=ALICE.address)
        await until("the mint to the named holder", lambda: stack.minted_to(ALICE.address), want=MILLION)
        assert await stack.minted_to(DEPLOYER.address) == 0


class TestAttestors:
    """Three attestors against a threshold of two."""

    async def test_one_short_of_the_threshold_mints_nothing(self, stack):
        """One signature is not a mint however long the relayer waits; the second attestor's
        arrival completes the transfer with nothing resent."""
        stack.services.attestor(0).relayer()
        await stack.lock(new_account().address, MILLION)

        await until("the one attestor to sign", lambda: stack.services.signed(0))
        await asyncio.sleep(IDLE_SECONDS)
        assert await stack.supply() == 0, "one attestor's signature minted"
        code, out = await stack.monitor()
        assert code == 0 and "locked but not yet minted" in out, out

        stack.services.attestor(1)
        await until("the mint once the threshold is met", stack.supply, want=MILLION)
        code, out = await stack.monitor()
        assert code == 0 and "escrow covers supply" in out, out

    async def test_a_revoked_one_still_answering_does_not_block_a_mint(self, w3, chain_id, stack):
        """The adapter reverts on any non-attestor's signature, so the relayer must leave it out."""
        adapter = stack.bridge.adapter
        role = await NVNM_BRIDGE_ADAPTER.fns.ATTESTOR_ROLE().call(w3, to=adapter)
        revoke = NVNM_BRIDGE_ADAPTER.fns.revokeRole(role, Account.from_key(ATTESTOR_KEYS[0]).address)
        await transact(w3, chain_id, DEPLOYER, adapter, revoke)

        stack.services.start()
        await stack.lock(new_account().address, MILLION)
        await until("the mint from the two still counted", stack.supply, want=MILLION)
        assert stack.services.signed(0) >= 1, "the revoked attestor signed, and was left out"


class TestRelayers:
    async def test_two_settle_each_transfer_once(self, stack):
        """They race for every transfer; `mint` is idempotent, and the loser must know it lost."""
        stack.services.start().relayer(SECOND_RELAYER_KEY, name="relayer-2")
        validator = new_account().address
        for _ in range(3):
            await stack.lock(validator, MILLION)

        await until("all three locks minted", stack.supply, want=3 * MILLION)
        await asyncio.sleep(IDLE_SECONDS)
        assert await stack.supply() == await stack.escrow() == 3 * MILLION

        # A duplicate that mined and reverted is a retry, not a mint.
        reported = sum(stack.services.log(name).count(" minted key=") for name in ("relayer", "relayer-2"))
        assert reported == 3, f"{reported} mints reported for 3 transfers"


class TestMonitor:
    async def test_it_tells_a_stray_from_a_break(self, w3, eth, chain_id, stack):
        """Nothing runs but the monitor; every state is made by hand."""
        bridge = stack.bridge

        # A stray transfer into the lockbox is not escrow, and must not page anyone.
        await bridge_mod.give_nvnm(eth, bridge, ALICE.address, MILLION)
        stray = ERC20.fns.transfer(bridge.lockbox, MILLION).data
        await bridge_mod.eth_send(eth, ALICE_KEY, to=bridge.nvnm, data=stray)
        code, out = await stack.monitor()
        assert code == 0 and "escrow covers supply" in out and "nobody locked" in out, out

        # In flight: locked, not yet minted, because nothing is relaying.
        await stack.lock(new_account().address, MILLION)
        code, out = await stack.monitor()
        assert code == 0 and "locked but not yet minted" in out, out

        # A break: supply minted outside the bridge, as a stolen threshold or a stray BRIDGE role would.
        await transact(w3, chain_id, DEPLOYER, bridge.token, BRIDGED_NVNM.fns.setRole(DEPLOYER.address, 1, True))
        await transact(w3, chain_id, DEPLOYER, bridge.token, BRIDGED_NVNM.fns.bridgeMint(DEPLOYER.address, 2 * MILLION))
        code, out = await stack.monitor()
        assert code == 1 and "L1 SUPPLY EXCEEDS ESCROW" in out, out
