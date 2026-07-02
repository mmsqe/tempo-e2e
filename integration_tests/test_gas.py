"""Gas estimation and accounting."""

from eth_contract.erc20 import ERC20
from tempo.constants import ALPHA_USD, PATH_USD

from .utils import new_account, send_calls, transfer_call


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
