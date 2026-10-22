"""TIP-403 transfer policies: whitelist/blacklist authorization"""

import pytest
from eth_utils import to_checksum_address
from hexbytes import HexBytes
from tempo.constants import TIP403_REGISTRY_ADDRESS as REGISTRY

from .abi import TIP403
from .utils import BLACKLIST, WHITELIST, call_revert, new_account, send_call

pytestmark = pytest.mark.tempo


async def test_whitelist_authorizes_only_members(w3, chain_id, funded_account):
    admin = funded_account
    policy_id = await TIP403.fns.policyIdCounter().call(w3)  # id of the next policy
    await send_call(w3, chain_id, admin, REGISTRY, TIP403.fns.createPolicy(admin.address, WHITELIST).data)
    policy_type, policy_admin = await TIP403.fns.policyData(policy_id).call(w3)
    assert policy_type == WHITELIST and HexBytes(policy_admin) == HexBytes(admin.address)

    member, outsider = new_account().address, new_account().address
    await send_call(w3, chain_id, admin, REGISTRY, TIP403.fns.modifyPolicyWhitelist(policy_id, member, True).data)
    assert await TIP403.fns.isAuthorized(policy_id, member).call(w3)
    assert not await TIP403.fns.isAuthorized(policy_id, outsider).call(w3)


async def test_blacklist_blocks_only_listed_accounts(w3, chain_id, funded_account):
    admin = funded_account
    policy_id = await TIP403.fns.policyIdCounter().call(w3)
    await send_call(w3, chain_id, admin, REGISTRY, TIP403.fns.createPolicy(admin.address, BLACKLIST).data)

    blocked, allowed = new_account().address, new_account().address
    await send_call(w3, chain_id, admin, REGISTRY, TIP403.fns.modifyPolicyBlacklist(policy_id, blocked, True).data)
    # A blacklist authorizes everyone except its restricted accounts.
    assert not await TIP403.fns.isAuthorized(policy_id, blocked).call(w3)
    assert await TIP403.fns.isAuthorized(policy_id, allowed).call(w3)


async def test_policy_membership_rejects_virtual_addresses(w3, chain_id, funded_account):
    """TIP-1022: a virtual address is a forwarding alias, never a valid policy member."""
    admin = funded_account
    policy_id = await TIP403.fns.policyIdCounter().call(w3)
    await send_call(w3, chain_id, admin, REGISTRY, TIP403.fns.createPolicy(admin.address, WHITELIST).data)

    virtual = to_checksum_address(b"\xaa\xbb\xcc\xdd" + b"\xfd" * 10 + b"\x00" * 6)
    data = TIP403.fns.modifyPolicyWhitelist(policy_id, virtual, True).data
    assert "VirtualAddressNotAllowed" in await call_revert(w3, REGISTRY, data, sender=admin.address)
