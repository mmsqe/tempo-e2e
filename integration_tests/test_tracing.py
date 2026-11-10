"""debug_ and trace_ tracing of tempo txs."""

import pytest
from hexbytes import HexBytes

from .utils import (
    RETURN_42_INIT,
    RETURN_42_RUNTIME,
    deploy_contract,
    init_code,
    new_account,
    rpc_result,
    send_call,
    send_calls,
    transfer_call,
)

pytestmark = pytest.mark.tempo

SPIN_RUNTIME = bytes.fromhex("5b600056")  # JUMPDEST PUSH1 0 JUMP: loops until it runs out of gas
DESTRUCT_RUNTIME = bytes.fromhex("5fff")  # PUSH0 SELFDESTRUCT
# TODO: remove once the fork takes the evm2 batch-tracing fix.
BATCH_TRACING_UNFIXED = pytest.mark.skip(
    reason="tempo's evm2 traces only a batch's last call, and trace_replayTransaction fails on a batch"
)


def _frames(trace: dict) -> list[dict]:
    """A callTracer trace's frames, root first."""
    return [trace, *(frame for call in trace.get("calls", []) for frame in _frames(call))]


def _vm_ops(vm_trace: dict | None) -> list[str | None]:
    """A Parity vmTrace's opcodes, each subcall's right after the op that made it; None for an entry without one."""
    ops = []
    for op in (vm_trace or {}).get("ops", []):
        ops += [op.get("op"), *_vm_ops(op.get("sub"))]
    return ops


class TestBatch:
    """Every call of a batch shows in each tracer."""

    @pytest.fixture
    async def batch(self, w3, chain_id, funded_account):
        """A create and a call, sent alone and then as one batch: the called address and the three tx hashes."""
        pk = funded_account.key.hex()
        _, address = await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=RETURN_42_INIT)
        create, call = {"to": None, "data": RETURN_42_INIT}, {"to": address, "data": b""}
        hashes = []
        for calls in ([create], [call], [create, call]):
            receipt = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=calls)
            hashes.append(receipt["transactionHash"].to_0x_hex())
        return address, hashes

    @BATCH_TRACING_UNFIXED
    async def test_call_tracer(self, w3, funded_account, batch):
        """Each call is a frame, whether the tracer roots it or nests it."""
        address, (*_, both) = batch
        trace = await rpc_result(w3, "debug_traceTransaction", [both, {"tracer": "callTracer"}])
        assert HexBytes(trace["from"]) == HexBytes(funded_account.address)
        frames = [(frame["type"], (frame.get("to") or "").lower()) for frame in _frames(trace)]
        assert any(kind == "CREATE" for kind, _ in frames), frames
        assert ("CALL", address.lower()) in frames, frames

    @BATCH_TRACING_UNFIXED
    async def test_call_tracer_marks_the_failing_call(self, w3, chain_id, funded_account):
        """A call that fails reverts its batch: it and the root carry the error, the call before it none."""
        pk = funded_account.key.hex()
        ok, failing = [
            (await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=init_code(runtime)))[1]
            for runtime in (RETURN_42_RUNTIME, SPIN_RUNTIME)
        ]
        calls = [{"to": address, "data": b""} for address in (ok, failing)]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=calls)
        assert receipt["status"] == 0
        trace = await rpc_result(
            w3, "debug_traceTransaction", [receipt["transactionHash"].to_0x_hex(), {"tracer": "callTracer"}]
        )
        errors = {frame["to"].lower(): frame.get("error") for frame in _frames(trace)[1:]}
        assert trace.get("error") and errors == {ok.lower(): None, failing.lower(): "out of gas"}, trace

    @BATCH_TRACING_UNFIXED
    async def test_struct_logs(self, w3, batch):
        _, hashes = batch
        traces = [await rpc_result(w3, "debug_traceTransaction", [tx_hash, {}]) for tx_hash in hashes]
        create, call, both = [[(log["op"], log["depth"]) for log in trace["structLogs"]] for trace in traces]
        assert create and call, "a single call's struct logs"
        assert both == create + call, "a batch's calls run at the depth of a call sent alone"

    @BATCH_TRACING_UNFIXED
    async def test_vm_trace(self, w3, batch):
        """A batch's root runs no opcode itself: it lists an entry without one per call, holding that call's."""
        _, hashes = batch
        replays = [await rpc_result(w3, "trace_replayTransaction", [tx_hash, ["vmTrace"]]) for tx_hash in hashes]
        create, call, both = [_vm_ops(replay["vmTrace"]) for replay in replays]
        assert create and call, "a single call's vmTrace"
        assert both == [None, *create, None, *call]

    async def test_the_root_reports_its_tx(self, w3, funded_account, batch):
        """Alone or in a batch, the root carries the tx's sender, gas and outcome under both tracers."""
        for tx_hash in batch[1]:
            gas_used = (await w3.eth.get_transaction_receipt(tx_hash))["gasUsed"]
            root = await rpc_result(w3, "debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
            assert HexBytes(root["from"]) == HexBytes(funded_account.address)
            assert int(root["gasUsed"], 16) == gas_used and "error" not in root, root
            default = await rpc_result(w3, "debug_traceTransaction", [tx_hash, {}])
            assert default["gas"] == gas_used and not default["failed"]

    async def test_a_self_destruct_is_traced_once(self, w3, chain_id, funded_account):
        """The root takes its last call's outcome, yet destroys nothing itself."""
        pk = funded_account.key.hex()
        _, doomed = await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=init_code(DESTRUCT_RUNTIME))
        calls = [{"to": None, "data": RETURN_42_INIT}, {"to": doomed, "data": b""}]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=calls)
        assert receipt["status"] == 1
        tx_hash = receipt["transactionHash"].to_0x_hex()
        trace = await rpc_result(w3, "debug_traceTransaction", [tx_hash, {"tracer": "callTracer"}])
        destroyed = [frame["from"].lower() for frame in _frames(trace) if frame["type"] == "SELFDESTRUCT"]
        assert destroyed == [doomed.lower()], trace
        replay = await rpc_result(w3, "trace_replayTransaction", [tx_hash, ["trace"]])
        suicides = [entry["action"]["address"].lower() for entry in replay["trace"] if entry["type"] == "suicide"]
        assert suicides == [doomed.lower()], replay


class TestTransfer:
    """A TIP-20 transfer's trace."""

    @pytest.fixture
    async def receipt(self, w3, chain_id, funded_account):
        return await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))

    async def test_struct_logger_responds(self, w3, receipt):
        trace = await rpc_result(w3, "debug_traceTransaction", [receipt["transactionHash"].to_0x_hex(), {}])
        assert {"gas", "failed", "returnValue", "structLogs"} <= set(trace.keys())
        assert isinstance(trace["structLogs"], list)

    async def test_its_block_traces_it(self, w3, receipt):
        block = hex(receipt["blockNumber"])
        traces = await rpc_result(w3, "debug_traceBlockByNumber", [block, {"tracer": "callTracer"}])
        assert receipt["transactionHash"].to_0x_hex() in [trace["txHash"] for trace in traces]
