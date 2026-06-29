"""Unmodified type-2 tx pays gas in default stablecoin."""

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import DEFAULT_MAX_PRIORITY_FEE_PER_GAS, new_account, suggested_max_fee

pytestmark = pytest.mark.tempo

STANDARD_TX_GAS = 1_000_000  # a transfer to a fresh recipient writes a new slot (TIP-1060 state gas)


async def test_type2_transfer_pays_gas_in_stablecoin(w3, chain_id, funded_account):
    recipient = new_account().address
    tx = {
        "to": PATH_USD,
        "data": ERC20.fns.transfer(recipient, 4321).data,
        "value": 0,
        "nonce": await w3.eth.get_transaction_count(funded_account.address),
        "chainId": chain_id,
        "gas": STANDARD_TX_GAS,
        "maxFeePerGas": await suggested_max_fee(w3),
        "maxPriorityFeePerGas": DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
        "type": 2,
    }
    signed = Account.sign_transaction(tx, funded_account.key)
    receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(signed.raw_transaction))

    assert receipt["status"] == 1
    assert receipt["type"] == 2  # a plain EIP-1559 tx, not the 0x76 AA type
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 4321
