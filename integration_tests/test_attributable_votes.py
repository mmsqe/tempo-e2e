"""Validators whose votes carry the signature of their vote key keep finalizing, serve those votes
and hold no evidence against one another. Nobody moves the entry of a validator that has a vote
key, which is how evidence reaches its bond; a validator still rotates its own key. A validator
registers its vote key once, from its own address, and the registry reads evidence of two
conflicting votes that key signed and names the validator. A key rotated away answers for a few
epochs more and for none after.

Needs ``--consensus`` and a tempo built from ``staking``."""

import json
import subprocess
import sys

import pytest
from Crypto.PublicKey import ECC
from eth_account import Account
from eth_utils import to_checksum_address
from tempo.constants import FEE_MANAGER_ADDRESS, VALIDATOR_CONFIG_V2_ADDRESS

from .abi import EQUIVOCATION, FEE, VALIDATOR_CONFIG_V2
from .conftest import localnet
from .evidence import FINALIZE, NOTARIZE, NULLIFY, against, ballot, double_notarize, registration
from .network import FAUCET_PRIVATE_KEY, free_port, resolve_tempo_bin
from .test_validator_config import NS_ROTATE, enrolment, join
from .utils import call_revert, connect, cs, fund_token, new_account, send_call, wait_for_block

pytestmark = pytest.mark.tempo

N_VALIDATORS = 4
EPOCH_LENGTH = 20
# The node's own: how many epochs past its deactivation a key still answers for.
RETIRED_KEY_GRACE = 4


@pytest.fixture(scope="module")
def signed_net(request, tmp_path_factory):
    """Four validators that sign their votes from genesis, where each has its vote key."""
    yield from localnet(
        request,
        tmp_path_factory,
        "attributable-votes",
        N_VALIDATORS,
        epoch_length=EPOCH_LENGTH,
        genesis={"t12Time": 0},
        flags=["--attributable-votes-time", "0"],
    )


@pytest.mark.consensus
@pytest.mark.slow
async def test_validators_serve_signed_votes_and_no_evidence(signed_net):
    urls = [signed_net.node_rpc_url(f"node{i}") for i in range(N_VALIDATORS)]
    w3 = connect(urls[0])
    try:
        # Into the second epoch: the key ceremony ran under the signed votes too.
        await wait_for_block(w3, EPOCH_LENGTH + 5)

        signers = set()
        for view in range(1, 6):
            held = await w3.provider.make_request("consensus_getVotes", [{"epoch": 1, "view": view}])
            signers |= {vote["signer"] for vote in held["result"]}
        assert len(signers) >= 3, signers
        assert (await w3.provider.make_request("consensus_getEquivocations", []))["result"] == []
    finally:
        await w3.provider.disconnect()

    # Comparing what every node received finds nothing either.
    command = [resolve_tempo_bin(), "consensus", "equivocations", "--quiet"]
    command += [arg for url in urls for arg in ("--rpc-url", url)]
    found = subprocess.run(command, capture_output=True, text=True, check=True)
    assert json.loads(found.stdout) == []


@pytest.mark.consensus
@pytest.mark.slow
async def test_nobody_moves_a_validators_entry(signed_net):
    """The address is where the bond is, and where evidence reaches it: the owner cannot move it either."""
    registry = VALIDATOR_CONFIG_V2_ADDRESS
    w3 = connect(signed_net.node_rpc_url("node0"))
    try:
        first, *_ = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry)
        validator, idx = cs(first[1]), first[5]
        owner = cs(await VALIDATOR_CONFIG_V2.fns.owner().call(w3, to=registry))
        move = VALIDATOR_CONFIG_V2.fns.transferValidatorOwnership(idx, new_account().address).data
        move = move if isinstance(move, str) else "0x" + bytes(move).hex()

        for sender in (validator, owner):
            assert "Unauthorized" in await call_revert(w3, registry, move, sender=sender)
    finally:
        await w3.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
async def test_a_validator_still_rotates_its_own_key(signed_net):
    """Rotation keeps the old key under the same address, so it needs no owner."""
    registry = VALIDATOR_CONFIG_V2_ADDRESS
    w3 = connect(signed_net.node_rpc_url("node0"))
    try:
        first, *_ = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry)
        validator, idx = cs(first[1]), first[5]
        ingress = "10.9.9.9:26656"
        key, signature, egress, _ = enrolment(await w3.eth.chain_id, validator, ingress, namespace=NS_ROTATE)
        rotate = VALIDATOR_CONFIG_V2.fns.rotateValidator(idx, key, ingress, egress, signature).data
        rotate = rotate if isinstance(rotate, str) else "0x" + bytes(rotate).hex()

        by_validator = await w3.provider.make_request(
            "eth_call", [{"from": validator, "to": registry, "data": rotate}, "latest"]
        )
        assert "error" not in by_validator, by_validator
    finally:
        await w3.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
async def test_the_registry_names_the_validator_evidence_convicts(signed_net, tmp_path):
    """Last in the file: it enrols a validator that never comes online."""
    registry, owner = VALIDATOR_CONFIG_V2_ADDRESS, Account.from_key(FAUCET_PRIVATE_KEY)
    url = signed_net.node_rpc_url("node0")
    w3 = connect(url)
    try:
        chain_id, genesis = await w3.eth.chain_id, bytes((await w3.eth.get_block(0))["hash"])
        token = to_checksum_address(await FEE.fns.userTokens(owner.address).call(w3, to=FEE_MANAGER_ADDRESS))
        newcomer, key = new_account(), ECC.generate(curve="ed25519")
        ingress = f"127.0.0.1:{free_port()}"
        pubkey, _ = await join(w3, chain_id, owner, newcomer.address, ingress, key=key, fee_token=token)
        epoch = await w3.eth.block_number // EPOCH_LENGTH
        signing_key = tmp_path / "signing.key"
        signing_key.write_text("0x" + key.seed.hex())

        equivocator, notarized_twice = EQUIVOCATION.fns.equivocator, double_notarize(key, chain_id, genesis, epoch)

        # Until the registry holds its vote key there is nothing to check its votes against.
        assert "ValidatorNotFound" in await call_revert(w3, registry, equivocator(notarized_twice).data)

        # The node derives the same vote key and proof from the key file as this suite does alone.
        index = (await VALIDATOR_CONFIG_V2.fns.validatorByAddress(newcomer.address).call(w3, to=registry))[5]
        vote_key, proof = registration(key, chain_id)
        set_vote_key = EQUIVOCATION.fns.setVoteKey(index, vote_key, proof).data
        command = [resolve_tempo_bin(), "consensus", "set-vote-key", "--quiet", "--dry-run"]
        command += ["--signing-key", str(signing_key), "--rpc-url", url]
        printed = subprocess.run(command, capture_output=True, text=True, check=True).stdout
        assert json.loads(printed)["input"] == "0x" + bytes(set_vote_key).hex()
        # Only its own address registers it: the owner can rewrite an entry's key, and would sign in its name.
        assert "Unauthorized" in await call_revert(w3, registry, set_vote_key, sender=owner.address)
        await fund_token(w3, chain_id=chain_id, to=newcomer.address, token=token, amount=1_000_000)
        await send_call(w3, chain_id, newcomer, registry, set_vote_key, fee_token=token)
        assert await EQUIVOCATION.fns.voteKey(pubkey).call(w3, to=registry) == vote_key
        # Once.
        assert "PublicKeyAlreadyExists" in await call_revert(w3, registry, set_vote_key, sender=newcomer.address)

        # Two proposals notarized in one round, signed for this chain.
        validator, at_epoch, view, _ = await equivocator(notarized_twice).call(w3, to=registry)
        assert (cs(validator), at_epoch, view) == (newcomer.address, epoch, 3)

        # The command line makes the same evidence from the node's key file.
        command = [sys.executable, "-m", "integration_tests.evidence", "--key", str(signing_key)]
        command += ["--epoch", str(epoch), "--rpc-url", url]
        printed = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        assert printed == "0x" + notarized_twice.hex()

        # So do two proposals finalized, and one finalized in a round the key nullified.
        def vote(kind, payload=0):
            return ballot(key, chain_id, genesis, epoch, kind, payload)

        for pair in [
            (vote(FINALIZE, 1), vote(FINALIZE, 2)),
            (vote(NULLIFY), vote(FINALIZE, 1)),
            (vote(FINALIZE, 1), vote(NULLIFY)),
        ]:
            validator, *_ = await equivocator(against(key, *pair)).call(w3, to=registry)
            assert cs(validator) == newcomer.address

        # What an honest signer casts convicts nobody, nor do votes for another chain, or one vote twice.
        for nothing in [
            against(key, vote(NOTARIZE, 1), vote(NULLIFY)),
            against(key, vote(NOTARIZE, 1), vote(FINALIZE, 2)),
            double_notarize(key, chain_id + 1, genesis, epoch),
            double_notarize(key, chain_id, bytes(32), epoch),
            double_notarize(key, chain_id, genesis, epoch, (1, 1)),
        ]:
            assert "InvalidSignature" in await call_revert(w3, registry, equivocator(nothing).data)

        # Rotated away, the key still answers under the same address through its grace, and no further.
        second = f"127.0.0.1:{free_port()}"
        new, signature, egress, _ = enrolment(chain_id, newcomer.address, second, namespace=NS_ROTATE)
        rotate = VALIDATOR_CONFIG_V2.fns.rotateValidator(index, new, second, egress, signature).data
        await fund_token(w3, chain_id=chain_id, to=newcomer.address, token=token, amount=10_000_000)
        receipt = await send_call(w3, chain_id, newcomer, registry, rotate, fee_token=token)
        last = receipt["blockNumber"] // EPOCH_LENGTH + RETIRED_KEY_GRACE
        await wait_for_block(w3, (last + 1) * EPOCH_LENGTH)
        in_time, late = (equivocator(double_notarize(key, chain_id, genesis, at)) for at in (last, last + 1))
        validator, at_epoch, *_ = await in_time.call(w3, to=registry)
        assert (cs(validator), at_epoch) == (newcomer.address, last)
        assert "ValidatorNotFound" in await call_revert(w3, registry, late.data)
    finally:
        await w3.provider.disconnect()
