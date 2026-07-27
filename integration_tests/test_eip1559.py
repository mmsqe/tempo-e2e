"""EIP-1559 fee market behavior: base fee, max-fee bounds, and priority-fee (tip) charging.

The native 0x76 tx carries ``max_priority_fee_per_gas``, and the node charges
``effective_gas_price = min(max_fee, base_fee + priority)``.
"""

import pytest
from tempo import Signer, serialize, sign_transaction

from .utils import (
    build_tempo_tx,
    gas_cost_in_token,
    new_account,
    send_calls,
    suggested_max_fee,
    transfer_call,
)


async def _base_fee(w3, block: int | str = "latest") -> int:
    return (await w3.eth.get_block(block))["baseFeePerGas"]


async def _transfer(w3, chain_id, acct, *, priority, max_fee):
    """A successful TIP-20 transfer with explicit fee-market params, via the standard send path."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=acct.key.hex(),
        calls=[transfer_call(new_account().address, 1)],
        max_priority_fee_per_gas=priority,
        max_fee_per_gas=max_fee,
    )
    assert receipt["status"] == 1
    return receipt


async def test_effective_gas_price_within_max_fee(w3, chain_id, funded_account):
    max_fee = await suggested_max_fee(w3)
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 1)]
    )
    assert receipt["status"] == 1
    assert receipt["effectiveGasPrice"] <= max_fee


async def test_max_fee_below_base_fee_is_rejected(w3, chain_id, funded_account):
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        max_fee_per_gas=1,
        max_priority_fee_per_gas=1,
        calls=[transfer_call(new_account().address, 1)],
    )
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))


# -- priority fees (EIP-1559 tips) --------------------------------------------


async def test_effective_gas_price_includes_priority_tip(w3, chain_id, funded_account):
    """With ample max_fee, effective price == inclusion-block base fee + requested tip."""
    priority = 3_000_000_000
    base = await _base_fee(w3)
    assert base > 0
    receipt = await _transfer(w3, chain_id, funded_account, priority=priority, max_fee=base * 4 + priority)

    assert receipt["effectiveGasPrice"] == await _base_fee(w3, receipt["blockNumber"]) + priority


async def test_priority_fee_capped_by_max_fee(w3, chain_id, funded_account):
    """When max_fee < base + priority the tip is squeezed: effective price == max_fee."""
    base = await _base_fee(w3)
    # priority == max_fee is the largest tip EIP-1559 admission allows (priority <= max_fee),
    # yet the base fee must still be paid first, so the realized tip is squeezed below it.
    max_fee = base * 2  # stays >= base even after a +12.5% base-fee step
    receipt = await _transfer(w3, chain_id, funded_account, priority=max_fee, max_fee=max_fee)

    assert receipt["effectiveGasPrice"] == max_fee
    realized_tip = receipt["effectiveGasPrice"] - await _base_fee(w3, receipt["blockNumber"])
    assert 0 <= realized_tip < max_fee


async def test_higher_priority_fee_pays_more(w3, chain_id, funded_account):
    """A larger tip yields a larger realized tip and a larger stablecoin fee for identical work.

    This is the revenue signal a protocol take-rate would tap: more tip in ==
    more fee collected (today, entirely by the validator).
    """
    base = await _base_fee(w3)
    low_priority, high_priority = 1_000_000_000, 50_000_000_000  # gap dwarfs per-block base-fee drift
    max_fee = base * 4 + high_priority

    low = await _transfer(w3, chain_id, funded_account, priority=low_priority, max_fee=max_fee)
    high = await _transfer(w3, chain_id, funded_account, priority=high_priority, max_fee=max_fee)

    low_tip = low["effectiveGasPrice"] - await _base_fee(w3, low["blockNumber"])
    high_tip = high["effectiveGasPrice"] - await _base_fee(w3, high["blockNumber"])
    assert high_tip > low_tip
    assert gas_cost_in_token(high) > gas_cost_in_token(low)
