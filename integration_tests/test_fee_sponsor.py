"""Fee sponsorship: a fee payer covers gas for another account's tempo tx."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, add_fee_payer_signature, serialize, sign_transaction
from tempo.constants import PATH_USD

from .utils import build_tempo_tx, fund, gas_cost_in_token, get_nonce, new_account, suggested_max_fee, transfer_call

pytestmark = pytest.mark.tempo


async def _sponsored_raw(w3, chain_id, sender, payer, calls):
    """Serialize a tx from ``sender`` whose gas is sponsored by fee payer ``payer``."""
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=await get_nonce(w3, sender.address),
        fee_token=PATH_USD,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=calls,
        awaiting_fee_payer=True,
    )
    tx = sign_transaction(tx, Signer(sender.key.hex()))
    return serialize(add_fee_payer_signature(tx, Signer(payer.key.hex())))


async def test_fee_payer_covers_gas(w3, chain_id):
    sender, payer = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, payer.address)
    recipient = new_account().address
    sender_before = await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD)
    payer_before = await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD)

    raw = await _sponsored_raw(w3, chain_id, sender, payer, [transfer_call(recipient, 1000)])
    receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw))

    assert receipt["status"] == 1
    # Sender loses only the transfer amount; the fee payer eats the gas.
    assert await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD) == sender_before - 1000
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == payer_before - gas_cost_in_token(receipt)


async def test_unfunded_fee_payer_is_rejected(w3, chain_id):
    """The fee payer, not the sender, must afford the gas escrow -- a broke payer's
    sponsorship is rejected at admission even though the sender is funded."""
    sender, broke_payer = new_account(), new_account()
    await fund(w3, sender.address)

    raw = await _sponsored_raw(w3, chain_id, sender, broke_payer, [transfer_call(new_account().address, 1000)])
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(raw)
