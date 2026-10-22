"""Fee sponsorship: a fee payer covers gas for another account's tempo tx."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import funded, gas_cost_in_token, new_account, send_signed, sign_sponsored, transfer_call

pytestmark = pytest.mark.tempo


async def test_fee_payer_covers_gas(w3, chain_id):
    sender, payer = await funded(w3), await funded(w3)
    recipient = new_account().address
    sender_before = await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD)
    payer_before = await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD)

    receipt = await send_signed(w3, await sign_sponsored(w3, chain_id, sender, payer, [transfer_call(recipient, 1000)]))

    assert receipt["status"] == 1
    # Sender loses only the transfer amount; the fee payer eats the gas.
    assert await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD) == sender_before - 1000
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == payer_before - gas_cost_in_token(receipt)


async def test_unfunded_fee_payer_is_rejected(w3, chain_id):
    """The fee payer, not the sender, must afford the gas escrow, so a broke payer is rejected at admission."""
    sender, broke_payer = await funded(w3), new_account()

    signed = await sign_sponsored(w3, chain_id, sender, broke_payer, [transfer_call(new_account().address, 1000)])
    with pytest.raises(Exception):
        await send_signed(w3, signed)
