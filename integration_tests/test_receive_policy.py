"""TIP-1028 receive policies + ReceivePolicyGuard (0xB10C…, T6): a receiver sets
which senders/tokens it accepts; a rejected transfer (or mint) is escrowed in the
guard as a blocked receipt (not reverted) that the receiver claims or a role holder burns.
"""

import pytest
from eth_abi import decode
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_checksum_address
from tempo.constants import PATH_USD
from tempo.constants import TIP403_REGISTRY_ADDRESS as REGISTRY

from .abi import RECEIVE_POLICY_GUARD as GUARD
from .abi import TIP20, TIP20_ROLES, TIP403
from .utils import (
    STATE_WRITE_GAS,
    call_revert,
    create_token,
    fund,
    gas_cost_in_token,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo

GUARD_ADDR = to_checksum_address("0xB10C000000000000000000000000000000000000")
REJECT_ALL, ALLOW_ALL, BLACKLIST = 0, 1, 1  # built-in TIP-403 policy ids; PolicyType.BLACKLIST
RECEIVE_POLICY = 2  # BlockedReason
BURN_BLOCKED_ROLE = keccak(text="BURN_BLOCKED_ROLE")


def _blocked_receipt(receipt):
    """The escrow witness bytes from the guard's TransferBlocked log (data = amount, version, receipt)."""
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == GUARD_ADDR.lower())
    amount, _version, witness = decode(["uint256", "uint8", "bytes"], bytes(log["data"]))
    return amount, witness


async def _reject_all_senders(w3, chain_id, receiver):
    """Receiver adopts a policy that rejects every sender, recoverable by itself."""
    data = TIP403.fns.setReceivePolicy(REJECT_ALL, ALLOW_ALL, receiver.address).data
    await send_call(w3, chain_id, receiver, REGISTRY, data)


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
    blocked = await send_call(w3, chain_id, sender, PATH_USD, ERC20.fns.transfer(receiver.address, 4000).data)
    amount, witness = _blocked_receipt(blocked)
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

    blocked = await send_call(w3, chain_id, sender, token, ERC20.fns.transfer(receiver.address, 6000).data)
    amount, witness = _blocked_receipt(blocked)

    # without BURN_BLOCKED_ROLE the escrow cannot be burned
    reason = await call_revert(w3, GUARD_ADDR, GUARD.fns.burnBlockedReceipt(witness).data, sender=sender.address)
    assert "Unauthorized" in reason or "0x82b42900" in reason

    # bind the token to a policy that makes the subject (receiver) unauthorized as a sender,
    # a precondition for burning the escrowed funds
    pid = await TIP403.fns.policyIdCounter().call(w3, to=REGISTRY)
    await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": REGISTRY, "data": TIP403.fns.createPolicy(admin.address, BLACKLIST).data},
            {"to": REGISTRY, "data": TIP403.fns.modifyPolicyBlacklist(pid, receiver.address, True).data},
            {"to": token, "data": TIP20.fns.changeTransferPolicyId(pid).data},
        ],
    )

    supply_before = await ERC20.fns.totalSupply().call(w3, to=token)
    await send_call(w3, chain_id, admin, GUARD_ADDR, GUARD.fns.burnBlockedReceipt(witness).data)
    assert await GUARD.fns.balanceOf(witness).call(w3, to=GUARD_ADDR) == 0  # receipt consumed
    assert await ERC20.fns.balanceOf(GUARD_ADDR).call(w3, to=token) == 0  # escrow drained
    assert await ERC20.fns.totalSupply().call(w3, to=token) == supply_before - amount  # supply burned
