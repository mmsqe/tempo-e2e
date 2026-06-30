"""Paying gas in a chosen stablecoin via the tempo tx fee_token field."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, PATH_USD

from .utils import (
    build_tempo_tx,
    fund_token,
    gas_cost_in_token,
    new_account,
    send_tempo_tx,
    suggested_max_fee,
    transfer_call,
)

pytestmark = pytest.mark.tempo


# Gas in a non-default fee token is swapped via the FeeAMM, so the token needs
# genesis pool liquidity. ALPHA_USD has it; PATH_USD is the default fee token.
@pytest.mark.parametrize("token", [ALPHA_USD])
async def test_gas_paid_in_chosen_stablecoin(w3, chain_id, token):
    acct = new_account()
    await fund_token(w3, chain_id=chain_id, to=acct.address, token=token, amount=5_000_000)
    before = await ERC20.fns.balanceOf(acct.address).call(w3, to=token)
    recipient = new_account().address

    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        fee_token=token,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(recipient, 1000, token)],
    )
    receipt = await send_tempo_tx(w3, tx, acct.key.hex())

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=token) == 1000
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=token) == before - 1000 - gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=PATH_USD) == 0  # faucet token never involved


async def test_fee_token_without_balance_is_rejected(w3, chain_id, funded_account):
    """funded_account holds only PATH_USD; paying gas in ALPHA_USD must fail."""
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        fee_token=ALPHA_USD,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1)],
    )
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))
