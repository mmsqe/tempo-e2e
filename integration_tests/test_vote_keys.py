"""A chain that forks into attributable votes, as one whose validators had no vote key at genesis
does: they register ahead of the fork through `tempo consensus set-vote-key`. From the fork on only
their votes count, so a validator that has not registered sits out, and it votes again some epochs
after it does.

Needs ``--consensus`` and a tempo built from ``staking``."""

import asyncio
import subprocess
import time

import pytest
from tempo.constants import VALIDATOR_CONFIG_V2_ADDRESS

from .abi import EQUIVOCATION, VALIDATOR_CONFIG_V2
from .conftest import localnet
from .network import resolve_tempo_bin
from .test_fee_amm import dev_account
from .utils import connect, wait_for_block

pytestmark = pytest.mark.tempo

N_VALIDATORS = 4
EPOCH_LENGTH = 20
# Seconds from making the genesis to the fork, for a quorum to register in: short of one by then,
# the chain stops.
LEAD = 60


@pytest.fixture(scope="module")
def forking_net(request, tmp_path_factory):
    """Four validators without a vote key, and the time from which their votes are attributable."""
    fork = int(time.time()) + LEAD
    genesis = {"t12Time": 0, "attributableVotesTime": fork}
    nets = localnet(request, tmp_path_factory, "vote-keys", N_VALIDATORS, epoch_length=EPOCH_LENGTH, genesis=genesis)
    for net in nets:
        yield net, fork


@pytest.mark.consensus
@pytest.mark.slow
async def test_only_validators_with_a_vote_key_vote_from_the_fork_on(forking_net, tmp_path):
    net, fork = forking_net
    urls = [net.node_rpc_url(f"node{i}") for i in range(N_VALIDATORS)]
    nodes = [connect(url) for url in urls]
    w3 = nodes[0]

    async def register(node: int) -> int:
        """The validator's key file makes its registration and its own address sends it, account
        `node + 1` of the dev mnemonic. Returns the epoch it landed in."""
        home = net.data_dir / f"node{node}"
        wallet = tmp_path / f"validator{node}.key"
        wallet.write_text("0x" + bytes(dev_account(node + 1).key).hex())
        command = [resolve_tempo_bin(), "consensus", "set-vote-key", "--quiet", "--yes", "--rpc-url", urls[0]]
        command += ["--signing-key", str(home / "signing.key"), "--secret", str(home / ".secret")]
        sent = subprocess.run([*command, "--wallet-key", str(wallet)], capture_output=True, text=True, check=True)
        receipt = await w3.eth.wait_for_transaction_receipt(sent.stderr.split()[-1])
        assert receipt["status"] == 1
        return receipt["blockNumber"] // EPOCH_LENGTH

    async def signers(epoch: int) -> set[bytes]:
        """Whose votes the nodes hold for the first rounds of `epoch`, once it is under way."""
        await wait_for_block(w3, epoch * EPOCH_LENGTH + 8)
        found = set()
        for node in nodes:
            for view in range(1, 6):
                held = await node.provider.make_request("consensus_getVotes", [{"epoch": epoch, "view": view}])
                found |= {bytes.fromhex(vote["signer"][2:]) for vote in held["result"]}
        return found

    async def timestamp() -> int:
        # Raw, because a boundary block's extraData is past what web3.py's formatter takes.
        latest = await w3.provider.make_request("eth_getBlockByNumber", ["latest", False])
        return int(latest["result"]["timestamp"], 16)

    try:
        # Before the fork the chain finalizes on the threshold votes, which name nobody.
        assert await signers(0) == set()
        for node in range(3):
            await register(node)
        assert await timestamp() < fork, "three validators did not register ahead of the fork"

        # Two epochs past the fork, each has started from a boundary stamped after it: the three
        # sign and finalize, and the fourth's votes do not count.
        while await timestamp() < fork:
            await asyncio.sleep(1)
        epoch = await w3.eth.block_number // EPOCH_LENGTH + 2
        registry = VALIDATOR_CONFIG_V2_ADDRESS
        validators = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry)
        three = {bytes(v[0]) for v in validators if await EQUIVOCATION.fns.voteKey(v[0]).call(w3, to=registry)}
        assert len(three) == 3 and await signers(epoch) == three

        # Once it registers the next boundary seats it, and it votes from the epoch its share is for.
        assert len(await signers(await register(3) + 3)) == N_VALIDATORS
    finally:
        for node in nodes:
            await node.provider.disconnect()
