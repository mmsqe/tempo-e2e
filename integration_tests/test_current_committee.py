"""TIP-1070 current committee precompile (T8+): an epoch's last block applies its DKG outcome by a
system call, so ``getCommitteeMembers`` reads consensus state and only address 0 may write it."""

import pytest
from hexbytes import HexBytes

from .abi import CURRENT_COMMITTEE as COMMITTEE
from .abi import CURRENT_COMMITTEE_ADDRESS as COMMITTEE_ADDRESS
from .utils import call_revert, wait_for_block

pytestmark = pytest.mark.tempo

ZERO_KEY = HexBytes(bytes(32))


async def test_dev_node_precompile_is_deployed_and_empty(w3):
    """The address carries the 0xEF precompile marker; a dev node runs no DKG, so the committee stays zero."""
    assert HexBytes(await w3.eth.get_code(COMMITTEE_ADDRESS)) == HexBytes("0xef")

    epoch, keys = await COMMITTEE.fns.getCommitteeMembers().call(w3)
    assert (epoch, len(keys)) == (0, 0)


async def test_set_committee_members_is_system_only(w3, account):
    """Only the system caller (address 0) may write the committee; an EOA gets Unauthorized()."""
    data = COMMITTEE.fns.setCommitteeMembers(1, [ZERO_KEY]).data
    assert "Unauthorized" in await call_revert(w3, COMMITTEE_ADDRESS, data, sender=account.address)


@pytest.mark.slow
@pytest.mark.consensus
async def test_committee_is_written_at_the_epoch_boundary(consensus_w3, consensus_net, num_validators):
    """The boundary block, ``(number + 1) % epoch_length == 0``, applies its own DKG outcome: the
    committee is empty at its parent and holds every validator's key at the boundary."""
    boundary = consensus_net.config.epoch_length - 1
    await wait_for_block(consensus_w3, boundary)

    members = COMMITTEE.fns.getCommitteeMembers()
    epoch, keys = await members.call(consensus_w3, block_identifier=boundary - 1)
    assert (epoch, len(keys)) == (0, 0), "committee written before the epoch boundary"

    epoch, keys = await members.call(consensus_w3, block_identifier=boundary)
    assert epoch >= 1, "boundary block did not apply the DKG outcome"
    assert len(set(keys)) == num_validators, "expected one distinct key per validator"
    assert ZERO_KEY not in {HexBytes(key) for key in keys}

    # The write is persistent state, not a per-block artifact: it is still there at head.
    assert await members.call(consensus_w3) == (epoch, keys)
