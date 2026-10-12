"""Stake-weighted block proposers on a localnet that crosses T12:
from the fork each epoch's leaders are drawn by the stake its DKG outcome records.

Needs ``--consensus`` and a tempo built from ``staking``."""

import time
from collections import Counter

import pytest
from eth_account import Account
from tempo.constants import PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS

from .abi import STAKING, VALIDATOR_CONFIG_V2
from .conftest import LOCALNET_CHAIN_ID, localnet
from .network import FAUCET_PRIVATE_KEY
from .staking import ETHER, STAKING_GENESIS_ADDRESS, STAKING_SALT, deploy
from .utils import connect, cs, latest_timestamp, wait_for_block, wait_for_timestamp

pytestmark = pytest.mark.tempo

# The 20% cap is raised to 1/N, so with five seats or fewer every seat would draw alike.
N_VALIDATORS = 7
EPOCH_LENGTH = 50
COUNTED_EPOCHS = 6
# Seconds from setup to T12: enough for the net to start, not for it to finish staking.
FORK_DELAY = 120
# By ascending address. Under the cap: 20, 20, 15, 15, 15, 7.5 and 7.5% of the rounds.
STAKES = [30, 30, 10, 10, 10, 5, 5]


@pytest.fixture(scope="module")
def t12_time():
    return int(time.time()) + FORK_DELAY


@pytest.fixture(scope="module")
def weighted_net(request, tmp_path_factory, t12_time):
    """Seven validators whose genesis names the staking proxy, with T12 shortly after the start."""
    genesis = {"stakingElection": STAKING_GENESIS_ADDRESS, "t12Time": t12_time}
    yield from localnet(
        request, tmp_path_factory, "weighted-proposers", N_VALIDATORS, epoch_length=EPOCH_LENGTH, genesis=genesis
    )


@pytest.fixture
async def w3(weighted_net):
    client = connect(weighted_net.node_rpc_url("node0"))
    yield client
    await client.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
async def test_proposers_follow_stake(w3, t12_time):
    """Past T12 the heaviest seats propose more blocks than the lightest; uniform draws would
    not tell them apart. From T12 a block pays its proposer's recipient, here its own address."""
    assert await latest_timestamp(w3) < t12_time, "the net started past T12; raise FORK_DELAY"

    faucet = Account.from_key(FAUCET_PRIVATE_KEY)
    staking = await deploy(w3, LOCALNET_CHAIN_ID, faucet, PATH_USD, salt=STAKING_SALT)
    assert staking.address == STAKING_GENESIS_ADDRESS, "proxy != genesis stakingElection address"

    active = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
    assert len(active) == N_VALIDATORS
    assert all(cs(v[4]) == cs(v[1]) for v in active), "each validator is paid at its own address"
    validators = sorted(cs(v[1]) for v in active)

    await staking.setup_election(faucet, validators)
    for validator, stake in zip(validators, STAKES, strict=True):
        await staking.stake(faucet, validator, stake * ETHER)
    assert list(await staking.call(STAKING.fns.electionWeight(validators))) == [s * ETHER for s in STAKES]
    staked = await w3.eth.block_number

    await wait_for_timestamp(w3, t12_time)
    forked = await w3.eth.block_number

    # The next boundary weighs the epoch after it; count from the one after that, to be safe.
    first = (max(staked, forked) // EPOCH_LENGTH + 2) * EPOCH_LENGTH
    last = first + COUNTED_EPOCHS * EPOCH_LENGTH - 1
    await wait_for_block(w3, last, timeout=1_800)
    proposers = Counter([cs((await w3.eth.get_block(n))["miner"]) for n in range(first, last + 1)])
    counts = [proposers[v] for v in validators]

    top, bottom = counts[:2], counts[-2:]
    assert min(top) > max(bottom), counts
    assert sum(top) > 1.5 * sum(bottom), counts
