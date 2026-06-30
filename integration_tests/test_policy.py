"""TIP-403 transfer policies: whitelist/blacklist authorization"""

import pytest
from hexbytes import HexBytes
from tempo.constants import TIP403_REGISTRY_ADDRESS as REGISTRY

from .abi import TIP403
from .utils import STATE_WRITE_GAS, new_account, send_calls

pytestmark = pytest.mark.tempo

WHITELIST, BLACKLIST = 0, 1  # ITIP403Registry.PolicyType


async def _registry_tx(w3, chain_id, account, data):
    calls = [{"to": REGISTRY, "data": data}]
    return await send_calls(
        w3, chain_id=chain_id, private_key=account.key.hex(), gas_limit=STATE_WRITE_GAS, calls=calls
    )


async def _authorized(w3, policy_id, user) -> bool:
    return await TIP403.fns.isAuthorized(policy_id, user).call(w3, to=REGISTRY)


async def test_whitelist_authorizes_only_members(w3, chain_id, funded_account):
    admin = funded_account
    policy_id = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY)  # id of the next policy
    created = await _registry_tx(w3, chain_id, admin, TIP403.fns.createPolicy(admin.address, WHITELIST).data)
    assert created["status"] == 1
    policy_type, policy_admin = await TIP403.fns.policyData(policy_id).call(w3, to=REGISTRY)
    assert policy_type == WHITELIST and HexBytes(policy_admin) == HexBytes(admin.address)

    member, outsider = new_account().address, new_account().address
    added = await _registry_tx(w3, chain_id, admin, TIP403.fns.modifyPolicyWhitelist(policy_id, member, True).data)
    assert added["status"] == 1
    assert await _authorized(w3, policy_id, member)
    assert not await _authorized(w3, policy_id, outsider)


async def test_blacklist_blocks_only_listed_accounts(w3, chain_id, funded_account):
    admin = funded_account
    policy_id = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY)
    created = await _registry_tx(w3, chain_id, admin, TIP403.fns.createPolicy(admin.address, BLACKLIST).data)
    assert created["status"] == 1

    blocked, allowed = new_account().address, new_account().address
    listed = await _registry_tx(w3, chain_id, admin, TIP403.fns.modifyPolicyBlacklist(policy_id, blocked, True).data)
    assert listed["status"] == 1
    # A blacklist authorizes everyone except its restricted accounts.
    assert not await _authorized(w3, policy_id, blocked)
    assert await _authorized(w3, policy_id, allowed)
