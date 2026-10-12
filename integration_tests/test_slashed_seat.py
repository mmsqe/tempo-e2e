"""Evidence slashing on a network whose epochs are real and whose committee is elected: evidence
counts for `evidenceEpochs` epochs, a key pays once in each, and a validator slashed below the
election's floor loses its seat, with as much of its bond seized on Ethereum.

Four validators run. A fifth stands on a bond bridged in and never comes online, so its
conflicting votes are signed by hand.

Needs ``--consensus``, anvil and a tempo built from ``staking``."""

import asyncio
import os

import pytest
from Crypto.PublicKey import ECC
from eth_account import Account
from tempo.constants import PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS

from .abi import (
    CURRENT_COMMITTEE,
    CURRENT_COMMITTEE_ADDRESS,
    EQUIVOCATION,
    NVNM_LOCKBOX,
    STAKING,
    TIP20_ROLES,
    VALIDATOR_CONFIG_V2,
)
from .anvil import MILLION, RELAYER_KEY
from .bridge import DEPLOYER, deploy, give_nvnm, lock, relay
from .conftest import LOCALNET_CHAIN_ID, localnet
from .evidence import double_notarize, registration
from .network import FAUCET_PRIVATE_KEY, free_port
from .staking import STAKING_GENESIS_ADDRESS, STAKING_SALT
from .staking import deploy as deploy_staking
from .test_bridge import account, candidates
from .test_fee_amm import dev_account
from .test_validator_config import join
from .utils import ISSUER_ROLE, connect, create_token, fund, rejects, send_call, until, wait_for_block

pytestmark = pytest.mark.tempo

N_VALIDATORS = 4
EPOCH_LENGTH = 20
# A tenth of the bond, for evidence one epoch old at most.
BPS, EVIDENCE_EPOCHS = 1_000, 1


@pytest.fixture(scope="module")
def net(request, tmp_path_factory):
    """Four validators that sign their votes from genesis, under the staking contract's election."""
    genesis = {"t12Time": 0, "stakingElection": STAKING_GENESIS_ADDRESS}
    flags = ["--attributable-votes-time", "0"]
    yield from localnet(
        request, tmp_path_factory, "slashed-seat", N_VALIDATORS, epoch_length=EPOCH_LENGTH, genesis=genesis, flags=flags
    )


@pytest.fixture
async def w3(net):
    client = connect(net.node_rpc_url("node0"))
    yield client
    await client.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
async def test_a_validator_slashed_below_the_floor_loses_its_seat(w3, eth, monkeypatch):
    # A localnet has no faucet call, so the helpers fund from this key: the deployer's own here.
    monkeypatch.setenv("E2E_FUNDER_KEY", FAUCET_PRIVATE_KEY)
    chain_id, registry = LOCALNET_CHAIN_ID, VALIDATOR_CONFIG_V2_ADDRESS
    await fund(w3, Account.from_key(RELAYER_KEY).address)

    # The bridge, around a staking contract at the address the genesis names for the election.
    salt = os.urandom(32)
    token = await create_token(w3, chain_id=chain_id, admin=DEPLOYER, name="NVNM", currency="NVNM", salt=salt)
    staking = await deploy_staking(w3, chain_id, DEPLOYER, reward_token=PATH_USD, stake_token=token, salt=STAKING_SALT)
    assert staking.address == STAKING_GENESIS_ADDRESS
    b = await deploy(eth, w3, staking=staking)
    await staking.send(DEPLOYER, TIP20_ROLES.fns.grantRole(ISSUER_ROLE, b.token), to=token)
    await staking.send(DEPLOYER, STAKING.fns.setBondGateway(b.mint_gateway))
    relays = [asyncio.create_task(relay(b.eth, b.l1)), asyncio.create_task(relay(b.l1, b.eth))]
    try:

        async def committee():
            _, keys = await CURRENT_COMMITTEE.fns.getCommitteeMembers().call(w3, to=CURRENT_COMMITTEE_ADDRESS)
            return {bytes(k) for k in keys}

        four = await committee()

        # Five seats, each for a full bond: the floor a slashed one falls under.
        bond = b.units(MILLION)
        await staking.setup_election(DEPLOYER, [], seats=N_VALIDATORS + 1, unbonding=2)
        await staking.send(DEPLOYER, STAKING.fns.setCandidacyBond(bond))
        await staking.send(DEPLOYER, STAKING.fns.setMinAcquired(bond))
        await staking.send(DEPLOYER, STAKING.fns.setEquivocation(BPS, EVIDENCE_EPOCHS))

        # Each running validator bonds from its own address: the dev mnemonic's accounts after the first.
        running = [dev_account(i + 1) for i in range(N_VALIDATORS)]
        for validator in running:
            await give_nvnm(b, validator.address, MILLION)
            await lock(b, validator.key.hex(), MILLION)

        # The fifth: enrolled, its vote key registered, bonded.
        v, key = await account(b), ECC.generate(curve="ed25519")
        pubkey, _ = await join(w3, chain_id, DEPLOYER, v.address, f"127.0.0.1:{free_port()}", key=key)
        index = (await VALIDATOR_CONFIG_V2.fns.validatorByAddress(v.address).call(w3, to=registry))[5]
        set_vote_key = EQUIVOCATION.fns.setVoteKey(index, *registration(key, chain_id))
        await send_call(w3, chain_id, v, registry, set_vote_key.data)
        await lock(b, v.key.hex(), MILLION)

        async def standing():
            return set(await candidates(b))

        await until("the bonds to stand their validators", standing, want={a.address for a in (*running, v)})
        await until("the fifth validator to take a seat", committee, want=four | {bytes(pubkey)}, timeout=300)

        reporter = await account(b)
        genesis = bytes((await w3.eth.get_block(0))["hash"])
        l1_bond, eth_bond = STAKING.fns.bondOf(v.address), NVNM_LOCKBOX.fns.bondOf(v.address)

        def slash(epoch, view=3):
            return STAKING.fns.slashEquivocation(double_notarize(key, chain_id, genesis, epoch, view=view))

        # Two epochs back is past the window, however good the evidence.
        epoch = await w3.eth.block_number // EPOCH_LENGTH
        await rejects(w3, staking.address, slash(epoch - 2), "EvidenceExpired", sender=reporter.address)

        # This epoch's costs a tenth, here and on Ethereum, and no other round of it costs more.
        await staking.send(reporter, slash(epoch))
        await rejects(w3, staking.address, slash(epoch, view=4), "AlreadySlashed", sender=reporter.address)
        assert await staking.call(l1_bond) == bond * 9 // 10
        await until("the seizure on Ethereum", lambda: eth_bond.call(eth, to=b.lockbox), want=MILLION * 9 // 10)

        # A later epoch is another offence.
        await wait_for_block(w3, (epoch + 1) * EPOCH_LENGTH)
        await staking.send(reporter, slash(await w3.eth.block_number // EPOCH_LENGTH))
        assert await staking.call(l1_bond) == bond * 81 // 100

        # Under the floor it does not stand at the next election: the four are the committee again.
        await until("the committee to drop it", committee, want=four, timeout=300)
    finally:
        for task in relays:
            task.cancel()
        await asyncio.gather(*relays, return_exceptions=True)
