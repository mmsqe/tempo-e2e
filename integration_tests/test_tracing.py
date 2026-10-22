"""debug_traceTransaction: a tempo tx's real work (CREATE/CALL) appears as callTracer subcalls."""

import pytest
from hexbytes import HexBytes

from .utils import RETURN_42_INIT, deploy_contract, new_account, rpc_result, send_call, send_calls, transfer_call

pytestmark = pytest.mark.requires("tempo-native")


async def test_call_tracer_shows_create_and_call_subcalls(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    deploy_receipt, address = await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=RETURN_42_INIT)
    deploy_trace = await rpc_result(
        w3, "debug_traceTransaction", [deploy_receipt["transactionHash"].to_0x_hex(), {"tracer": "callTracer"}]
    )
    assert HexBytes(deploy_trace["from"]) == HexBytes(funded_account.address)
    assert any(call["type"] == "CREATE" for call in deploy_trace.get("calls", []))

    call_receipt = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=[{"to": address, "data": b""}])
    call_trace = await rpc_result(
        w3, "debug_traceTransaction", [call_receipt["transactionHash"].to_0x_hex(), {"tracer": "callTracer"}]
    )
    assert any(
        call["type"] == "CALL" and HexBytes(call["to"]) == HexBytes(address) for call in call_trace.get("calls", [])
    )


async def test_struct_logger_responds(w3, chain_id, funded_account):
    receipt = await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))
    trace = await rpc_result(w3, "debug_traceTransaction", [receipt["transactionHash"].to_0x_hex(), {}])
    assert {"gas", "failed", "returnValue", "structLogs"} <= set(trace.keys())
    assert isinstance(trace["structLogs"], list)


async def test_trace_block_by_number_covers_its_txs(w3, chain_id, funded_account):
    receipt = await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))
    traces = await rpc_result(w3, "debug_traceBlockByNumber", [hex(receipt["blockNumber"]), {"tracer": "callTracer"}])
    assert len(traces) >= 1  # the block holds at least the tx we sent
    assert "result" in traces[0]
