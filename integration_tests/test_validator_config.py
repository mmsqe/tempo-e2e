"""TIP-1017 ValidatorConfig V2 (0xCccC…01): an append-only validator registry.
The dev genesis seeds it initialized at height 0 with zero validators, owned by
the prefunded dev account, so the read surface and owner gating are assertable.
"""

import pytest
from eth_account import Account
from eth_utils import to_checksum_address
from tempo.constants import VALIDATOR_CONFIG_V2_ADDRESS as V2_ADDR

from .abi import VALIDATOR_CONFIG_V2 as V2
from .network import FAUCET_PRIVATE_KEY
from .utils import call_revert, new_account

pytestmark = pytest.mark.tempo


async def test_dev_genesis_registry_state(w3):
    """The V2 registry is live from genesis: initialized at height 0, empty, dev-owned."""
    assert await V2.fns.isInitialized().call(w3, to=V2_ADDR)
    assert await V2.fns.getInitializedAtHeight().call(w3, to=V2_ADDR) == 0
    assert await V2.fns.validatorCount().call(w3, to=V2_ADDR) == 0
    assert not await V2.fns.getActiveValidators().call(w3, to=V2_ADDR)
    assert await V2.fns.getNextNetworkIdentityRotationEpoch().call(w3, to=V2_ADDR) == 0
    owner = await V2.fns.owner().call(w3, to=V2_ADDR)
    assert to_checksum_address(owner) == Account.from_key(FAUCET_PRIVATE_KEY).address  # the prefunded dev account


async def test_mutators_are_owner_gated(w3):
    outsider = new_account()
    add = V2.fns.addValidator(outsider.address, b"\x11" * 32, "1.2.3.4:26656", "", outsider.address, b"")
    assert "Unauthorized" in await call_revert(w3, V2_ADDR, add.data, sender=outsider.address)
    handoff = V2.fns.transferOwnership(outsider.address)
    assert "Unauthorized" in await call_revert(w3, V2_ADDR, handoff.data, sender=outsider.address)
