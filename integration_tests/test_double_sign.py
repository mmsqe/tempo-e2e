"""A validator put back on an old copy of its data votes again in a round it already voted in:
the double sign an operator makes by restoring a backup. The node that has both votes holds the
evidence, logs and counts it, the command line prints it, and the registry names the validator.
No vote here is signed by hand.

Needs ``--consensus`` and a tempo built from ``staking``."""

import json
import os
import re
import shutil
import signal
import subprocess
import urllib.request

import pytest
from tempo.constants import VALIDATOR_CONFIG_V2_ADDRESS
from tempo.devnet.ports import consensus_metrics_port

from .abi import EQUIVOCATION, VALIDATOR_CONFIG_V2
from .conftest import localnet
from .network import resolve_tempo_bin
from .test_fee_amm import dev_account
from .utils import connect, cs, until, wait_for_block

pytestmark = pytest.mark.tempo

N_VALIDATORS = 4
# One epoch for the whole test: both votes are of one round.
EPOCH_LENGTH = 100_000
# A round outwaits the freeze the copy is taken in, so it still finalizes. And no node repeats a
# nullify: with the repeat goes the certificate that opened its round, which would take the
# restored node there before it votes in its old one.
NODE_FLAGS = [
    "--consensus.wait-for-proposal=5s",
    "--consensus.wait-for-notarizations=6s",
    "--consensus.wait-to-rebroadcast-nullify=10m",
    "--consensus.inactive-time-before-leader-skip=11m",
]


@pytest.fixture(scope="module")
def net(request, tmp_path_factory):
    """Four validators that sign their votes from genesis."""
    yield from localnet(
        request,
        tmp_path_factory,
        "double-sign",
        N_VALIDATORS,
        epoch_length=EPOCH_LENGTH,
        genesis={"t12Time": 0},
        flags=["--attributable-votes-time", "0"],
        node_flags=NODE_FLAGS,
    )


@pytest.mark.consensus
@pytest.mark.slow
async def test_a_validator_restored_from_an_old_copy_signs_twice(net):
    witness, restored, others = "node0", "node1", ("node2", "node3")
    home, copy = net.data_dir / restored, net.data_dir / "copy"
    w3 = connect(net.node_rpc_url(witness))
    try:
        await wait_for_block(w3, await w3.eth.block_number + 20)

        # The validator's data at one instant: every node is frozen for the copy, so the round they
        # are in is still open when they go on, and this validator finalizes it.
        pids = [process["pid"] for process in net.status()]
        for pid in pids:
            os.kill(pid, signal.SIGSTOP)
        try:
            shutil.copytree(home, copy)
        finally:
            for pid in pids:
                os.kill(pid, signal.SIGCONT)
        await wait_for_block(w3, await w3.eth.block_number + 10)

        # The mistake: the validator goes back to the copy. Only the witness is up beside it, so
        # nothing takes it past the round the copy left it in.
        for name in (restored, *others):
            net.stop_node(name)
        shutil.rmtree(home)
        copy.rename(home)
        net.start_node(restored)

        async def held():
            return (await w3.provider.make_request("consensus_getEquivocations", []))["result"]

        evidence, *_ = await until("the witness to hold evidence", held, timeout=60)

        # It is this validator's: node 1 is the dev mnemonic's third account.
        registry, validator = VALIDATOR_CONFIG_V2_ADDRESS, dev_account(2).address
        active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry)
        (key,) = [bytes(v[0]) for v in active if cs(v[1]) == validator]
        named, epoch, view, _ = await EQUIVOCATION.fns.equivocator(bytes.fromhex(evidence[2:])).call(w3, to=registry)
        assert (cs(named), epoch, evidence[:66]) == (validator, 0, "0x" + key.hex())

        # The command line makes the same of what the witness received.
        url = net.node_rpc_url(witness)
        command = [resolve_tempo_bin(), "consensus", "equivocations", "--quiet", "--rpc-url", url]
        found, *_ = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        assert found == {"signer": "0x" + key.hex(), "epoch": 0, "view": view, "evidence": evidence}

        # The witness says so where an operator looks: in its log, and in its metrics' count.
        log = (net.data_dir / witness / "node.log").read_text()
        assert f"a validator signed conflicting votes signer=0x{key.hex()}" in log
        node = net.config.validators[0]
        metrics = urllib.request.urlopen(f"http://{node.host}:{consensus_metrics_port(node.base_port)}/metrics")
        assert re.search(rb"^\S*equivocations_found\S* [1-9]", metrics.read(), re.M), "no evidence counted"

        # The others come back and the chain goes on: three of four are enough.
        stopped_at = await w3.eth.block_number
        for name in others:
            net.start_node(name)
        await wait_for_block(w3, stopped_at + 3, timeout=120)
    finally:
        await w3.provider.disconnect()
