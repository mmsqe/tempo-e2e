"""Validators whose votes carry their signer's own signature keep finalizing, serve those votes
and hold no evidence against one another. From then on only the registry owner moves a
validator's entry, which is how evidence reaches its bond; a validator still rotates its own key.
The registry reads evidence of two conflicting votes and names the validator that signed them.

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
from .evidence import double_notarize
from .network import FAUCET_PRIVATE_KEY, free_port, resolve_tempo_bin
from .test_validator_config import NS_ROTATE, enrolment, join
from .utils import call_revert, connect, cs, new_account, wait_for_block

pytestmark = pytest.mark.tempo

N_VALIDATORS = 4
EPOCH_LENGTH = 20


@pytest.fixture(scope="module")
def signed_net(request, tmp_path_factory):
    """Four validators that sign their votes from genesis."""
    genesis = {"t12Time": 0, "attributableVotesTime": 0}
    yield from localnet(
        request, tmp_path_factory, "attributable-votes", N_VALIDATORS, epoch_length=EPOCH_LENGTH, genesis=genesis
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
async def test_only_the_owner_moves_a_validators_entry(signed_net):
    registry = VALIDATOR_CONFIG_V2_ADDRESS
    w3 = connect(signed_net.node_rpc_url("node0"))
    try:
        first, *_ = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry)
        validator, idx = cs(first[1]), first[5]
        owner = cs(await VALIDATOR_CONFIG_V2.fns.owner().call(w3, to=registry))
        move = VALIDATOR_CONFIG_V2.fns.transferValidatorOwnership(idx, new_account().address).data
        move = move if isinstance(move, str) else "0x" + bytes(move).hex()

        assert "Unauthorized" in await call_revert(w3, registry, move, sender=validator)
        by_owner = await w3.provider.make_request("eth_call", [{"from": owner, "to": registry, "data": move}, "latest"])
        assert "error" not in by_owner, by_owner
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
        await join(w3, chain_id, owner, newcomer.address, f"127.0.0.1:{free_port()}", key=key, fee_token=token)
        epoch = await w3.eth.block_number // EPOCH_LENGTH

        def evidence(chain_id, genesis, first, second):
            return EQUIVOCATION.fns.equivocator(double_notarize(key, chain_id, genesis, epoch, (first, second)))

        # Two proposals notarized in one round, signed for this chain.
        validator, at_epoch, view, _ = await evidence(chain_id, genesis, 1, 2).call(w3, to=registry)
        assert (cs(validator), at_epoch, view) == (newcomer.address, epoch, 3)

        # The command line makes the same evidence from the node's key file.
        signing_key = tmp_path / "signing.key"
        signing_key.write_text("0x" + key.seed.hex())
        command = [sys.executable, "-m", "integration_tests.evidence", "--key", str(signing_key)]
        command += ["--epoch", str(epoch), "--rpc-url", url]
        printed = subprocess.run(command, capture_output=True, text=True, check=True).stdout.strip()
        assert printed == "0x" + double_notarize(key, chain_id, genesis, epoch).hex()

        # The same votes signed for another chain, or the same vote twice, convict nobody.
        for nothing in [
            evidence(chain_id + 1, genesis, 1, 2),
            evidence(chain_id, bytes(32), 1, 2),
            evidence(chain_id, genesis, 1, 1),
        ]:
            assert "InvalidSignature" in await call_revert(w3, registry, nothing.data)
    finally:
        await w3.provider.disconnect()
