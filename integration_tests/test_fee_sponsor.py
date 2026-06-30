"""Fee sponsorship: a fee payer covers gas for another account's tempo tx.

xfail: tempo-py 0.1.0 fee-payer (0x78) signing is not accepted by the current node build.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, add_fee_payer_signature, serialize, sign_transaction
from tempo.constants import PATH_USD

from .utils import build_tempo_tx, fund, gas_cost_in_token, get_nonce, new_account, suggested_max_fee, transfer_call

pytestmark = pytest.mark.tempo


@pytest.mark.xfail(reason="tempo-py 0.1.0 fee-payer encoding not accepted by current node build", strict=False)
async def test_fee_payer_covers_gas(w3, chain_id):
    sender, payer = new_account(), new_account()
    await fund(w3, sender.address)
    await fund(w3, payer.address)
    recipient = new_account().address
    sender_before = await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD)
    payer_before = await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD)

    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=await get_nonce(w3, sender.address),
        fee_token=PATH_USD,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(recipient, 1000)],
    )
    tx = sign_transaction(tx, Signer(sender.key.hex()))
    tx = add_fee_payer_signature(tx, Signer(payer.key.hex()))
    receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(serialize(tx)))

    assert receipt["status"] == 1
    # Sender loses only the transfer amount; the fee payer eats the gas.
    assert await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD) == sender_before - 1000
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == payer_before - gas_cost_in_token(receipt)
