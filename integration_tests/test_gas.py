"""Gas estimation and accounting, incl. TIP-1000 state-creation costs and the
TIP-1010 per-transaction gas cap.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import ALPHA_USD, PATH_USD

from .utils import fund, new_account, send_calls, transfer_call

SSTORE_CREATE_COST = 250_000  # TIP-1000: creating a new state element
TX_GAS_CAP = 30_000_000  # TIP-1010: per-transaction gas limit cap


async def _transfer_gas(w3, chain_id, sender, recipient):
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=sender.key.hex(), calls=[transfer_call(recipient, 100)]
    )
    assert receipt["status"] == 1
    return receipt["gasUsed"]


async def test_estimate_gas_for_transfer(w3, funded_account):
    data = ERC20.fns.transfer(new_account().address, 100).data
    estimate = await w3.eth.estimate_gas({"from": funded_account.address, "to": PATH_USD, "data": data})
    assert estimate > 21_000


async def test_estimate_gas_for_0x76_honors_fee_token(w3, funded_account):
    """eth_estimateGas accepts a 0x76 tx and estimates against the tx's own fee token."""
    data = "0x" + ERC20.fns.transfer(new_account().address, 1).data.hex()

    async def estimate(fee_token: str) -> int:
        tx = {
            "from": funded_account.address,
            "to": PATH_USD,
            "value": "0x0",
            "data": data,
            "type": "0x76",
            "feeToken": fee_token,
        }
        resp = await w3.provider.make_request("eth_estimateGas", [tx])
        assert "error" not in resp, resp.get("error")
        return int(resp["result"], 16)

    # Both the default and a non-default fee token estimate successfully (fee-token-aware).
    assert await estimate(PATH_USD) > 21_000
    assert await estimate(ALPHA_USD) > 21_000


async def test_receipt_reports_gas_used(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 100)]
    )
    assert receipt["status"] == 1
    assert 0 < receipt["gasUsed"] <= receipt["cumulativeGasUsed"]


async def test_effective_gas_price_covers_base_fee(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 100)]
    )
    base_fee = (await w3.eth.get_block(receipt["blockNumber"])).get("baseFeePerGas") or 0
    assert receipt["effectiveGasPrice"] >= base_fee


async def test_new_balance_slot_costs_state_creation(w3, chain_id, funded_account):
    """TIP-1000: a transfer creating the recipient's balance slot costs ~250k more
    than the same transfer to a recipient whose slot already exists."""
    fresh = new_account().address
    await _transfer_gas(w3, chain_id, funded_account, funded_account.address)  # warm the sender's account first
    creating = await _transfer_gas(w3, chain_id, funded_account, fresh)  # creates the balance slot
    updating = await _transfer_gas(w3, chain_id, funded_account, fresh)  # slot now exists
    delta = creating - updating
    assert SSTORE_CREATE_COST * 0.94 <= delta <= SSTORE_CREATE_COST, delta


async def test_first_tx_pays_account_creation(w3, chain_id, funded_account):
    """TIP-1000: a sender's first tx (nonce 0) additionally pays account creation.
    The recipient's balance slot pre-exists, so the sender's account entry is the only diff."""
    sender, recipient = new_account(), funded_account.address
    await fund(w3, sender.address)  # creates the balance slot but NOT the account (nonce) entry
    first = await _transfer_gas(w3, chain_id, sender, recipient)  # nonce 0 -> 1: creates the account entry
    second = await _transfer_gas(w3, chain_id, sender, recipient)  # existing account
    delta = first - second
    # account creation (250k) plus the new-nonce-slot premium over an existing one
    assert SSTORE_CREATE_COST <= delta <= SSTORE_CREATE_COST + 30_000, delta


async def test_tx_gas_limit_cap_rejected(w3, chain_id, funded_account):
    """TIP-1010: a tx demanding more than the 30M per-tx cap is rejected at admission."""
    with pytest.raises(Exception, match="gas"):
        await send_calls(
            w3,
            chain_id=chain_id,
            private_key=funded_account.key.hex(),
            gas_limit=TX_GAS_CAP + 1,
            calls=[transfer_call(new_account().address, 1)],
        )
