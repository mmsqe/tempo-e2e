"""TIP-1028 receive policies + ReceivePolicyGuard (0xB10C…, T6): a receiver sets
which senders/tokens it accepts; a rejected transfer (or mint) is escrowed in the
guard as a blocked receipt (not reverted) that the receiver claims or a role holder burns.
"""

import pytest
from eth_abi import decode
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_checksum_address
from tempo.constants import PATH_USD
from tempo.constants import RECEIVE_POLICY_GUARD_ADDRESS as GUARD_ADDR
from tempo.constants import TIP403_REGISTRY_ADDRESS as REGISTRY

from .abi import RECEIVE_POLICY_GUARD as GUARD
from .abi import TIP20_ROLES, TIP403
from .utils import (
    STATE_WRITE_GAS,
    WHITELIST,
    approve_call,
    blacklist_token,
    call_revert,
    create_token,
    fund,
    gas_cost_in_token,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo


ZERO_ADDR = "0x" + "00" * 20
REJECT_ALL, ALLOW_ALL = 0, 1  # built-in TIP-403 policy ids
TOKEN_FILTER, RECEIVE_POLICY = 1, 2  # BlockedReason
BURN_BLOCKED_ROLE = keccak(text="BURN_BLOCKED_ROLE")


def _blocked_receipt(receipt):
    """The escrow witness bytes from the guard's TransferBlocked log (data = amount, version, receipt)."""
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == GUARD_ADDR.lower())
    amount, _version, witness = decode(["uint256", "uint8", "bytes"], bytes(log["data"]))
    return amount, witness


async def _block_transfer(w3, chain_id, sender, receiver, amount, token=PATH_USD):
    """Send a transfer the receiver's policy blocks; return (amount, witness)."""
    blocked = await send_call(w3, chain_id, sender, token, ERC20.fns.transfer(receiver.address, amount).data)
    return _blocked_receipt(blocked)


async def _set_receive_policy(w3, chain_id, receiver, sender_id, token_id, recovery):
    data = TIP403.fns.setReceivePolicy(sender_id, token_id, recovery).data
    await send_call(w3, chain_id, receiver, REGISTRY, data)


async def _reject_all_senders(w3, chain_id, receiver):
    """Receiver adopts a policy that rejects every sender, recoverable by itself."""
    await _set_receive_policy(w3, chain_id, receiver, REJECT_ALL, ALLOW_ALL, receiver.address)


async def _whitelist_policy(w3, chain_id, admin, member):
    """Create a WHITELIST policy containing ``member``; return its id."""
    pid = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY)
    await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": REGISTRY, "data": TIP403.fns.createPolicy(admin.address, WHITELIST).data},
            {"to": REGISTRY, "data": TIP403.fns.modifyPolicyWhitelist(pid, member, True).data},
        ],
    )
    return pid


async def _claim(w3, chain_id, receiver, witness):
    return await send_call(w3, chain_id, receiver, GUARD_ADDR, GUARD.fns.claim(receiver.address, witness).data)


async def test_blocked_transfer_is_escrowed_and_claimable(w3, chain_id):
    sender, receiver = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, receiver.address)
    await _reject_all_senders(w3, chain_id, receiver)
    assert await TIP403.fns.validateReceivePolicy(PATH_USD, sender.address, receiver.address).call(w3, to=REGISTRY) == (
        False,
        RECEIVE_POLICY,
    )

    receiver_before = await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD)
    guard_before = await ERC20.fns.balanceOf(GUARD_ADDR).call(w3, to=PATH_USD)
    amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 4000)
    assert amount == 4000
    # funds sit in the guard, not with the receiver
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD) == receiver_before
    assert await ERC20.fns.balanceOf(GUARD_ADDR).call(w3, to=PATH_USD) == guard_before + 4000
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 4000

    # a non-receiver cannot claim the escrow
    reason = await call_revert(w3, GUARD_ADDR, GUARD.fns.claim(sender.address, witness).data, sender=sender.address)
    assert "UnauthorizedClaimer" in reason or "0x5c4aa7dc" in reason

    claimed = await _claim(w3, chain_id, receiver, witness)  # the receiver claims it back to itself
    # receiver regains the 4000, less the gas it paid (in PATH_USD) to claim
    expected = receiver_before + 4000 - gas_cost_in_token(claimed)
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD) == expected
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 0  # receipt consumed


async def test_blocked_mint_is_escrowed_and_claimable(w3, chain_id, funded_account):
    admin = funded_account  # the token issuer / minter (originator of the blocked mint)
    receiver = new_account()
    await fund(w3, receiver.address)
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 1_000_000))
    await _reject_all_senders(w3, chain_id, receiver)

    # a mint to a receiver whose policy rejects the minter is escrowed (kind=MINT), not reverted
    minted = await send_call(w3, chain_id, admin, token, ERC20.fns.mint(receiver.address, 7000).data)
    amount, witness = _blocked_receipt(minted)
    assert amount == 7000
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=token) == 0  # not delivered
    assert await ERC20.fns.balanceOf(GUARD_ADDR).call(w3, to=token) == 7000  # freshly minted to the guard
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 7000

    await _claim(w3, chain_id, receiver, witness)
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=token) == 7000
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 0


async def test_burn_blocked_receipt(w3, chain_id, funded_account):
    admin = funded_account  # issuer, burn-role holder, and policy admin
    sender, receiver = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, receiver.address)  # receiver signs its own setReceivePolicy tx
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(sender.address, 10_000))
    await send_call(w3, chain_id, admin, token, TIP20_ROLES.fns.grantRole(BURN_BLOCKED_ROLE, admin.address).data)
    await _reject_all_senders(w3, chain_id, receiver)

    amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 6000, token)

    # without BURN_BLOCKED_ROLE the escrow cannot be burned
    reason = await call_revert(w3, GUARD_ADDR, GUARD.fns.burnBlockedReceipt(witness).data, sender=sender.address)
    assert "Unauthorized" in reason or "0x82b42900" in reason

    # bind the token to a policy that makes the subject (receiver) unauthorized as a sender,
    # a precondition for burning the escrowed funds
    await blacklist_token(w3, chain_id=chain_id, admin=admin, token=token, blocked=receiver.address)

    supply_before = await ERC20.fns.totalSupply().call(w3, to=token)
    await send_call(w3, chain_id, admin, GUARD_ADDR, GUARD.fns.burnBlockedReceipt(witness).data)
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 0  # receipt consumed
    assert await ERC20.fns.balanceOf(GUARD_ADDR).call(w3, to=token) == 0  # escrow drained
    assert await ERC20.fns.totalSupply().call(w3, to=token) == supply_before - amount  # supply burned


async def test_token_filter_blocks_non_whitelisted_token(w3, chain_id, funded_account):
    admin = funded_account
    sender, receiver = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, receiver.address)
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(sender.address, 10_000))

    # receiver accepts only PATH_USD (token filter), from any sender
    token_filter = await _whitelist_policy(w3, chain_id, admin, PATH_USD)
    await _set_receive_policy(w3, chain_id, receiver, ALLOW_ALL, token_filter, receiver.address)
    assert await TIP403.fns.validateReceivePolicy(token, sender.address, receiver.address).call(w3, to=REGISTRY) == (
        False,
        TOKEN_FILTER,
    )

    # transferring the non-whitelisted token is escrowed with reason TOKEN_FILTER (not delivered)
    _amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 3000, token)
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=token) == 0
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 3000


async def test_sender_whitelist_allows_only_listed(w3, chain_id, funded_account):
    admin = funded_account
    allowed, blocked_sender, receiver = new_account(), new_account(), new_account()
    for acct in (allowed, blocked_sender, receiver):
        await fund(w3, acct.address)

    sender_filter = await _whitelist_policy(w3, chain_id, admin, allowed.address)
    await _set_receive_policy(w3, chain_id, receiver, sender_filter, ALLOW_ALL, receiver.address)

    # the whitelisted sender is credited
    before = await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD)
    await send_call(w3, chain_id, allowed, PATH_USD, ERC20.fns.transfer(receiver.address, 1000).data)
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD) == before + 1000

    # a non-whitelisted sender is escrowed, leaving the receiver's balance unchanged
    _amount, witness = await _block_transfer(w3, chain_id, blocked_sender, receiver, 2000)
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 2000
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD) == before + 1000


async def test_originator_recovery_reroutes(w3, chain_id):
    sender, receiver = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, receiver.address)
    # recoveryAuthority = 0 -> only the originator (sender) can claim, and the claim is a reroute
    await _set_receive_policy(w3, chain_id, receiver, REJECT_ALL, ALLOW_ALL, ZERO_ADDR)
    _amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 4000)

    # the receiver is not the recovery authority
    r1 = await call_revert(w3, GUARD_ADDR, GUARD.fns.claim(receiver.address, witness).data, sender=receiver.address)
    assert "UnauthorizedClaimer" in r1
    # rerouting back to the still-blocking receiver re-runs its receive policy -> PolicyForbids
    r2 = await call_revert(w3, GUARD_ADDR, GUARD.fns.claim(receiver.address, witness).data, sender=sender.address)
    assert "PolicyForbids" in r2

    # ...but rerouting to a policy-free destination (the sender itself) succeeds
    after_block = await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD)
    claimed = await send_call(w3, chain_id, sender, GUARD_ADDR, GUARD.fns.claim(sender.address, witness).data)
    expected = after_block + 4000 - gas_cost_in_token(claimed)  # the 4000 returns, less claim gas
    assert await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD) == expected
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 0


async def test_third_party_recovery_claim(w3, chain_id):
    sender, receiver, helper = new_account(), new_account(), new_account()
    for acct in (sender, receiver, helper):
        await fund(w3, acct.address)
    # recoveryAuthority = helper -> only the helper may claim
    await _set_receive_policy(w3, chain_id, receiver, REJECT_ALL, ALLOW_ALL, helper.address)
    _amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 4000)

    for who in (sender, receiver):  # neither the originator nor the receiver is the authority
        reason = await call_revert(w3, GUARD_ADDR, GUARD.fns.claim(who.address, witness).data, sender=who.address)
        assert "UnauthorizedClaimer" in reason

    # the helper claims to the receiver (third-party -> receiver is a resume); the helper pays gas
    receiver_before = await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD)
    await send_call(w3, chain_id, helper, GUARD_ADDR, GUARD.fns.claim(receiver.address, witness).data)
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD) == receiver_before + 4000
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 0


async def test_transfer_from_blocked_still_consumes_allowance(w3, chain_id):
    owner, spender, receiver = new_account(), new_account(), new_account()
    for acct in (owner, spender, receiver):
        await fund(w3, acct.address)
    await _reject_all_senders(w3, chain_id, receiver)

    await send_call(w3, chain_id, owner, **approve_call(spender.address, amount=3000))
    owner_before = await ERC20.fns.balanceOf(owner.address).call(w3, to=PATH_USD)
    receiver_before = await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD)
    # the spender pulls the owner's funds toward the receiver -> blocked, but the allowance is consumed
    data = ERC20.fns.transferFrom(owner.address, receiver.address, 3000).data
    blocked = await send_call(w3, chain_id, spender, PATH_USD, data)
    amount, witness = _blocked_receipt(blocked)
    assert amount == 3000
    assert await ERC20.fns.allowance(owner.address, spender.address).call(w3, to=PATH_USD) == 0  # allowance spent
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 3000  # escrowed
    assert await ERC20.fns.balanceOf(owner.address).call(w3, to=PATH_USD) == owner_before - 3000  # left the owner
    assert await ERC20.fns.balanceOf(receiver.address).call(w3, to=PATH_USD) == receiver_before  # not delivered


async def test_double_claim_reverts(w3, chain_id):
    sender, receiver = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, receiver.address)
    await _reject_all_senders(w3, chain_id, receiver)
    _amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 4000)

    await _claim(w3, chain_id, receiver, witness)  # first claim consumes the receipt
    reason = await call_revert(w3, GUARD_ADDR, GUARD.fns.claim(receiver.address, witness).data, sender=receiver.address)
    assert "InvalidReceipt" in reason or "0xc0098aac" in reason


async def test_direct_transfer_to_guard_reverts(w3):
    sender = new_account()
    await fund(w3, sender.address)
    # the guard is a reserved address -- a direct transfer to it reverts (funds only arrive via escrow)
    reason = await call_revert(w3, PATH_USD, ERC20.fns.transfer(GUARD_ADDR, 1).data, sender=sender.address)
    assert "AddressReserved" in reason


async def test_set_receive_policy_rejects_compound_and_virtual(w3, chain_id, funded_account):
    admin = funded_account
    receiver = new_account().address

    # a COMPOUND policy cannot be used as a receive-policy filter (only simple / built-in ids)
    compound = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY)
    await send_call(
        w3, chain_id, admin, REGISTRY, TIP403.fns.createCompoundPolicy(ALLOW_ALL, ALLOW_ALL, ALLOW_ALL).data
    )
    compound_data = TIP403.fns.setReceivePolicy(compound, ALLOW_ALL, receiver).data
    assert "InvalidReceivePolicyType" in await call_revert(w3, REGISTRY, compound_data, sender=receiver)

    # a virtual address may not set a receive policy
    virtual = to_checksum_address(b"\xaa\xbb\xcc\xdd" + b"\xfd" * 10 + b"\x00" * 6)
    data = TIP403.fns.setReceivePolicy(ALLOW_ALL, ALLOW_ALL, receiver).data
    assert "VirtualAddressNotAllowed" in await call_revert(w3, REGISTRY, data, sender=virtual)


async def test_resume_claim_enforces_token_transfer_policy(w3, chain_id, funded_account):
    admin = funded_account
    sender, receiver = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, receiver.address)
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(sender.address, 10_000))
    await _reject_all_senders(w3, chain_id, receiver)  # recoveryAuthority = receiver -> claim is a resume
    _amount, witness = await _block_transfer(w3, chain_id, sender, receiver, 6000, token)

    # after escrow, bind the token to a policy that blocks the receiver as a recipient
    await blacklist_token(w3, chain_id=chain_id, admin=admin, token=token, blocked=receiver.address)
    # resume skips the receive-policy recheck but still enforces the token's TIP-403 destination check
    reason = await call_revert(w3, GUARD_ADDR, GUARD.fns.claim(receiver.address, witness).data, sender=receiver.address)
    assert "PolicyForbids" in reason
