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


async def _token_with_policy(w3, chain_id, admin, cid, *, mint):
    """A fresh TIP-20 whose transfer policy is ``cid``, with issuer granted and ``mint=(holder, amount)`` minted."""
    token = await create_token(w3, chain_id=chain_id, admin=admin)
    holder, amount = mint
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, admin.address).data},
            {"to": token, "data": TIP20.fns.changeTransferPolicyId(cid).data},
            {"to": token, "data": ERC20.fns.mint(holder, amount).data},
        ],
    )
    assert receipt["status"] == 1
    return token


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

    token = await _token_with_policy(w3, chain_id, admin, cid, mint=(sender.address, 100_000))
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


async def test_create_compound_rejects_invalid_references(w3, chain_id, funded_account):
    """Every referenced sub-policy must exist and be simple -- a missing id fails
    PolicyNotFound and a compound id fails PolicyNotSimple (no nesting)."""
    admin = funded_account
    cid, _, _ = await _create_compound(w3, chain_id, admin, sender=admin.address, recipient=admin.address)
    missing = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY) + 999

    bad_ref = TIP403.fns.createCompoundPolicy(missing, ALLOW_ALL, ALLOW_ALL).data
    assert "PolicyNotFound" in await call_revert(w3, REGISTRY, bad_ref, sender=admin.address)
    nested = TIP403.fns.createCompoundPolicy(cid, ALLOW_ALL, ALLOW_ALL).data
    assert "PolicyNotSimple" in await call_revert(w3, REGISTRY, nested, sender=admin.address)


async def test_mint_policy_dispatches_separately_from_recipient(w3, chain_id, funded_account):
    """A mint checks mintRecipientPolicyId, not recipientPolicyId: an account outside the
    recipient whitelist can still be minted to when the mint policy allows everyone."""
    admin = funded_account
    outsider = new_account().address  # in no whitelist; mint policy is ALLOW_ALL
    cid, _, _ = await _create_compound(w3, chain_id, admin, sender=admin.address, recipient=admin.address)
    token = await _token_with_policy(w3, chain_id, admin, cid, mint=(outsider, 5000))  # mint -> ALLOW_ALL policy
    assert await ERC20.fns.balanceOf(outsider).call(w3, to=token) == 5000

    # ... but a plain transfer to the same account checks the recipient policy -> PolicyForbids
    mint_more = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": token, "data": ERC20.fns.mint(admin.address, 5000).data}],
    )
    assert mint_more["status"] == 1
    reason = await call_revert(w3, token, ERC20.fns.transfer(outsider, 1000).data, sender=admin.address)
    assert "PolicyForbids" in reason
