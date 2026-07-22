"""TIP-1011 access-key permissions: a non-admin key can be scoped to specific
calls (CallScope) and capped by a per-token spending limit (TokenLimit). An
out-of-scope call fails CallNotAllowed; an over-limit spend, SpendingLimitExceeded.
"""

import asyncio

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer
from tempo.constants import ALPHA_USD, PATH_USD
from tempo.contracts import ACCOUNT_KEYCHAIN as KC
from tempo.contracts import ACCOUNT_KEYCHAIN_ADDRESS as KC_ADDR
from tempo.keychain import CallScope, SelectorRule, TokenLimit, sign_tx_access_key

from .abi import KEYCHAIN_VIEWS
from .utils import (
    RETURN_42_INIT,
    approve_call,
    fund,
    fund_token,
    key_restrictions,
    latest_timestamp,
    new_account,
    prepare_tx,
    send_call,
    send_signed,
    sign_tx_registered_key,
    transfer_call,
)

pytestmark = pytest.mark.tempo

TRANSFER_SEL = bytes.fromhex("a9059cbb")  # transfer(address,uint256)


def _scoped(root, tx, **kwargs):
    return sign_tx_access_key(tx, new_account().key.hex(), Signer(root.key.hex()), is_admin=False, **kwargs)


async def test_scoped_key_allows_whitelisted_call(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    recipient = new_account().address

    tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, 1500)])
    signed = _scoped(root, tx, allowed_calls=(CallScope.transfer(PATH_USD),))
    assert (await send_signed(w3, signed))["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1500


async def test_scoped_key_rejects_out_of_scope_call(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    spender = new_account().address

    # the key may only transfer PATH_USD; an approve is out of scope -> CallNotAllowed
    tx = await prepare_tx(w3, chain_id, root, [approve_call(spender, amount=1)])
    signed = _scoped(root, tx, allowed_calls=(CallScope.transfer(PATH_USD),))
    assert (await send_signed(w3, signed))["status"] == 0  # batch fails atomically before any call runs
    assert await ERC20.fns.allowance(root.address, spender).call(w3, to=PATH_USD) == 0


async def test_spending_limit_allows_within_and_blocks_over(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    await fund_token(w3, chain_id=chain_id, to=root.address, token=ALPHA_USD, amount=1_000_000)
    within, over = new_account().address, new_account().address
    # cap ALPHA at 5000; give PATH ample room so only the ALPHA transfer is the constraint
    limits = (TokenLimit.create(ALPHA_USD, 5000), TokenLimit.create(PATH_USD, 10**12))

    tx = await prepare_tx(w3, chain_id, root, [transfer_call(within, 3000, ALPHA_USD)])
    assert (await send_signed(w3, _scoped(root, tx, limits=limits)))["status"] == 1
    assert await ERC20.fns.balanceOf(within).call(w3, to=ALPHA_USD) == 3000

    # over limit -> SpendingLimitExceeded (status 0); the recipient receives nothing
    tx2 = await prepare_tx(w3, chain_id, root, [transfer_call(over, 8000, ALPHA_USD)])
    assert (await send_signed(w3, _scoped(root, tx2, limits=limits)))["status"] == 0
    assert await ERC20.fns.balanceOf(over).call(w3, to=ALPHA_USD) == 0


async def test_selector_rule_binds_recipient(w3, chain_id):
    """A SelectorRule can pin a selector to specific recipients: transfer to the listed
    address passes, any other recipient fails CallNotAllowed."""
    root = new_account()
    await fund(w3, root.address)
    allowed, blocked = new_account().address, new_account().address
    scope = CallScope.with_selector(PATH_USD, TRANSFER_SEL, (SelectorRule.create(TRANSFER_SEL, (allowed,)),))

    tx = await prepare_tx(w3, chain_id, root, [transfer_call(allowed, 1500)])
    assert (await send_signed(w3, _scoped(root, tx, allowed_calls=(scope,))))["status"] == 1
    assert await ERC20.fns.balanceOf(allowed).call(w3, to=PATH_USD) == 1500

    tx2 = await prepare_tx(w3, chain_id, root, [transfer_call(blocked, 1500)])
    assert (await send_signed(w3, _scoped(root, tx2, allowed_calls=(scope,))))["status"] == 0
    assert await ERC20.fns.balanceOf(blocked).call(w3, to=PATH_USD) == 0


async def test_empty_scope_denies_all_calls(w3, chain_id):
    """allowed_calls=() is scoped deny-all (unlike None = unrestricted)."""
    root = new_account()
    await fund(w3, root.address)
    recipient = new_account().address
    tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, 100)])
    assert (await send_signed(w3, _scoped(root, tx, allowed_calls=())))["status"] == 0
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0


async def test_access_key_cannot_create_contracts(w3, chain_id):
    """An access-key-signed tx may not CREATE, even under an admin key -- rejected at admission."""
    root = new_account()
    await fund(w3, root.address)
    tx = await prepare_tx(w3, chain_id, root, [{"to": None, "data": bytes.fromhex(RETURN_42_INIT)}])
    signed = sign_tx_access_key(tx, new_account().key.hex(), Signer(root.key.hex()), is_admin=True)
    with pytest.raises(Exception, match="cannot use CREATE"):
        await send_signed(w3, signed)


async def test_periodic_limit_resets_each_window(w3, chain_id):
    """TIP-1011 periodic limits: spending caps refresh once the window rolls over."""
    root, key = new_account(), new_account()
    await fund(w3, root.address)
    await fund_token(w3, chain_id=chain_id, to=root.address, token=ALPHA_USD, amount=1_000_000)
    recipient = new_account().address

    # register the key on-chain: 5000 ALPHA per 5-second window (PATH ample, for gas)
    limits = [(ALPHA_USD, 5000, 5), (PATH_USD, 10**12, 0)]
    restrictions = key_restrictions(enforce_limits=True, limits=limits)
    await send_call(w3, chain_id, root, KC_ADDR, KC.fns.authorizeKey(key.address, 0, restrictions).data)

    async def spend(amount):
        tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, amount, ALPHA_USD)])
        return (await send_signed(w3, sign_tx_registered_key(tx, key.key.hex(), root.address)))["status"]

    assert await spend(3000) == 1  # within this window's 5000
    assert await spend(3000) == 0  # only 2000 left -> SpendingLimitExceeded

    remaining_fn = KEYCHAIN_VIEWS.fns.getRemainingLimitWithPeriod(root.address, key.address, ALPHA_USD)
    _remaining, period_end = await remaining_fn.call(w3, to=KC_ADDR)
    for _ in range(30):  # let the 1s-block chain cross the window boundary
        if await latest_timestamp(w3) > period_end:
            break
        await asyncio.sleep(0.5)
    assert await spend(3000) == 1  # a fresh window restores the full 5000
