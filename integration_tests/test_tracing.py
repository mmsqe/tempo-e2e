"""debug_traceTransaction: a tempo tx's real work (CREATE/CALL) appears as callTracer subcalls."""

from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo.constants import PATH_USD

from .utils import deploy_contract, new_account, send_calls

RETURN_42_INIT = "600a600c600039600a6000f3602a60005260206000f3"


async def _trace(w3, tx_hash, options):
    resp = await w3.provider.make_request("debug_traceTransaction", [tx_hash, options])
    assert "error" not in resp, resp.get("error")
    return resp["result"]


async def test_call_tracer_shows_create_and_call_subcalls(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    deploy_receipt, address = await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=RETURN_42_INIT)
    deploy_trace = await _trace(w3, deploy_receipt["transactionHash"].to_0x_hex(), {"tracer": "callTracer"})
    assert HexBytes(deploy_trace["from"]) == HexBytes(funded_account.address)
    assert any(call["type"] == "CREATE" for call in deploy_trace.get("calls", []))

    call_receipt = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=[{"to": address, "data": b""}])
    call_trace = await _trace(w3, call_receipt["transactionHash"].to_0x_hex(), {"tracer": "callTracer"})
    assert any(
        call["type"] == "CALL" and HexBytes(call["to"]) == HexBytes(address) for call in call_trace.get("calls", [])
    )


async def test_struct_logger_responds(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 1).data}],
    )
    trace = await _trace(w3, receipt["transactionHash"].to_0x_hex(), {})
    assert {"gas", "failed", "returnValue", "structLogs"} <= set(trace.keys())
    assert isinstance(trace["structLogs"], list)
