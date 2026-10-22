"""Paying gas in a chosen stablecoin via the tempo tx fee_token field."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, BETA_USD, PATH_USD, THETA_USD

from .utils import (
    fund_token,
    gas_cost_in_token,
    new_account,
    prepare_tx,
    seed_fee_pool,
    send_calls,
    transfer_call,
)

pytestmark = pytest.mark.tempo


# A non-default fee token swaps through the FeeAMM, so it needs a pool; only ALPHA_USD has one at genesis.
@pytest.mark.parametrize("token", [ALPHA_USD, BETA_USD, THETA_USD])
async def test_gas_paid_in_chosen_stablecoin(w3, chain_id, token):
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token)
    acct = new_account()
    await fund_token(w3, chain_id=chain_id, to=acct.address, token=token, amount=5_000_000)
    before = await ERC20.fns.balanceOf(acct.address).call(w3, to=token)
    recipient = new_account().address

    calls = [transfer_call(recipient, 1000, token)]
    receipt = await send_calls(w3, chain_id=chain_id, private_key=acct.key.hex(), fee_token=token, calls=calls)

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=token) == 1000
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=token) == before - 1000 - gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=PATH_USD) == 0  # faucet token never involved


async def test_fee_token_without_balance_is_rejected(w3, chain_id, funded_account):
    """funded_account holds only PATH_USD; paying gas in ALPHA_USD must fail."""
    calls = [transfer_call(new_account().address, 1)]
    tx = await prepare_tx(w3, chain_id, funded_account, calls, fee_token=ALPHA_USD)
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))
