"""Gas estimation and accounting."""

from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import new_account, send_calls, transfer_call


async def test_estimate_gas_for_transfer(w3, funded_account):
    data = ERC20.fns.transfer(new_account().address, 100).data
    estimate = await w3.eth.estimate_gas({"from": funded_account.address, "to": PATH_USD, "data": data})
    assert estimate > 21_000


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
