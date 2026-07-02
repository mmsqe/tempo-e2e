"""TIP-1015 compound transfer policies (TIP-403, T2+): a compound policy composes
three role-dispatched sub-policies (sender, recipient, mint), so a transfer checks
the sender and recipient against different policies.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import TIP403_REGISTRY_ADDRESS as REGISTRY

from .abi import TIP20, TIP20_ROLES, TIP403
from .utils import ISSUER_ROLE, STATE_WRITE_GAS, call_revert, create_token, fund, new_account, send_calls

pytestmark = pytest.mark.tempo

WHITELIST, ALLOW_ALL = 0, 1  # PolicyType.WHITELIST; ALLOW_ALL is built-in policy id 1


async def _create_compound(w3, chain_id, admin, *, sender, recipient):
    """Compose sender-whitelist(sender) + recipient-whitelist(recipient) + ALLOW_ALL mint."""
    base = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY)
    sender_pid, recip_pid, cid = base, base + 1, base + 2  # each create bumps the counter by 1
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": REGISTRY, "data": TIP403.fns.createPolicy(admin.address, WHITELIST).data},
            {"to": REGISTRY, "data": TIP403.fns.modifyPolicyWhitelist(sender_pid, sender, True).data},
            {"to": REGISTRY, "data": TIP403.fns.createPolicy(admin.address, WHITELIST).data},
            {"to": REGISTRY, "data": TIP403.fns.modifyPolicyWhitelist(recip_pid, recipient, True).data},
            {"to": REGISTRY, "data": TIP403.fns.createCompoundPolicy(sender_pid, recip_pid, ALLOW_ALL).data},
        ],
    )
    assert receipt["status"] == 1
    return cid, sender_pid, recip_pid


async def test_compound_policy_dispatches_by_role(w3, chain_id, funded_account):
    s, v = new_account().address, new_account().address
    cid, sender_pid, recip_pid = await _create_compound(w3, chain_id, funded_account, sender=s, recipient=v)

    assert await TIP403.fns.isAuthorizedSender(cid, s).call(w3, to=REGISTRY)
    assert not await TIP403.fns.isAuthorizedRecipient(cid, s).call(w3, to=REGISTRY)
    assert await TIP403.fns.isAuthorizedRecipient(cid, v).call(w3, to=REGISTRY)
    assert not await TIP403.fns.isAuthorizedSender(cid, v).call(w3, to=REGISTRY)
    # isAuthorized = sender && recipient, so a sender-only account isn't "authorized" overall
    assert not await TIP403.fns.isAuthorized(cid, s).call(w3, to=REGISTRY)
    assert await TIP403.fns.compoundPolicyData(cid).call(w3, to=REGISTRY) == (sender_pid, recip_pid, ALLOW_ALL)


async def test_compound_policy_enforced_on_transfer(w3, chain_id, funded_account):
    admin = funded_account
    sender = new_account()
    await fund(w3, sender.address)  # sender pays its own gas in PATH_USD
    recipient, outsider = new_account().address, new_account().address
    cid, _, _ = await _create_compound(w3, chain_id, admin, sender=sender.address, recipient=recipient)

    token = await create_token(w3, chain_id=chain_id, admin=admin)
    setup = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, admin.address).data},
            {"to": token, "data": TIP20.fns.changeTransferPolicyId(cid).data},
            {"to": token, "data": ERC20.fns.mint(sender.address, 100_000).data},
        ],
    )
    assert setup["status"] == 1
    assert await TIP20.fns.transferPolicyId().call(w3, to=token) == cid

    # authorized: whitelisted sender -> whitelisted recipient
    ok = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=sender.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": token, "data": ERC20.fns.transfer(recipient, 1000).data}],
    )
    assert ok["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=token) == 1000

    # unauthorized recipient (not in the recipient whitelist) -> PolicyForbids
    reason = await call_revert(w3, token, ERC20.fns.transfer(outsider, 1000).data, sender=sender.address)
    assert "PolicyForbids" in reason or "0x54cfe659" in reason
