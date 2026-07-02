"""TIP-1011 access-key permissions: a non-admin key can be scoped to specific
calls (CallScope) and capped by a per-token spending limit (TokenLimit). An
out-of-scope call fails CallNotAllowed; an over-limit spend, SpendingLimitExceeded.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize
from tempo.constants import ALPHA_USD, PATH_USD
from tempo.keychain import CallScope, TokenLimit, sign_tx_access_key

from .utils import (
    STATE_WRITE_GAS,
    build_tempo_tx,
    fund,
    fund_token,
    get_nonce,
    new_account,
    suggested_max_fee,
    transfer_call,
)

pytestmark = pytest.mark.tempo


async def _tx(w3, chain_id, root, *, calls):
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=await get_nonce(w3, root.address),
        fee_token=PATH_USD,
        gas_limit=STATE_WRITE_GAS,  # scope/limit validation adds intrinsic gas
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=calls,
    )


async def _send(w3, signed):
    return await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(serialize(signed)))


def _scoped(root, tx, **kwargs):
    return sign_tx_access_key(tx, new_account().key.hex(), Signer(root.key.hex()), is_admin=False, **kwargs)


async def test_scoped_key_allows_whitelisted_call(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    recipient = new_account().address

    tx = await _tx(w3, chain_id, root, calls=[transfer_call(recipient, 1500)])
    signed = _scoped(root, tx, allowed_calls=(CallScope.transfer(PATH_USD),))
    assert (await _send(w3, signed))["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1500


async def test_scoped_key_rejects_out_of_scope_call(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    spender = new_account().address

    # the key may only transfer PATH_USD; an approve is out of scope -> CallNotAllowed
    tx = await _tx(w3, chain_id, root, calls=[{"to": PATH_USD, "data": ERC20.fns.approve(spender, 1).data}])
    signed = _scoped(root, tx, allowed_calls=(CallScope.transfer(PATH_USD),))
    assert (await _send(w3, signed))["status"] == 0  # batch fails atomically before any call runs
    assert await ERC20.fns.allowance(root.address, spender).call(w3, to=PATH_USD) == 0


async def test_spending_limit_allows_within_and_blocks_over(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    await fund_token(w3, chain_id=chain_id, to=root.address, token=ALPHA_USD, amount=1_000_000)
    within, over = new_account().address, new_account().address
    # cap ALPHA at 5000; give PATH ample room so only the ALPHA transfer is the constraint
    limits = (TokenLimit.create(ALPHA_USD, 5000), TokenLimit.create(PATH_USD, 10**12))

    tx = await _tx(w3, chain_id, root, calls=[transfer_call(within, 3000, ALPHA_USD)])
    assert (await _send(w3, _scoped(root, tx, limits=limits)))["status"] == 1
    assert await ERC20.fns.balanceOf(within).call(w3, to=ALPHA_USD) == 3000

    # over limit -> SpendingLimitExceeded (status 0); the recipient receives nothing
    tx2 = await _tx(w3, chain_id, root, calls=[transfer_call(over, 8000, ALPHA_USD)])
    assert (await _send(w3, _scoped(root, tx2, limits=limits)))["status"] == 0
    assert await ERC20.fns.balanceOf(over).call(w3, to=ALPHA_USD) == 0
