"""debug_ and trace_ tracing of tempo txs."""

import pytest
from hexbytes import HexBytes

from .utils import (
    CALL_TRACER,
    DESTRUCT_RUNTIME,
    RETURN_42_INIT,
    RETURN_42_RUNTIME,
    SPIN_RUNTIME,
    call_frames,
    new_account,
    rpc_result,
    send_call,
    send_calls,
    trace_tx,
    transfer_call,
)

pytestmark = pytest.mark.tempo

# reth runs a JS tracer given as an object; it has none of geth's named ones (bigramTracer, ...).
STEP_TRACER = {
    "tracer": """{
        steps: [],
        step: function(log) {
            var top = log.stack.length() ? "0x" + log.stack.peek(0).toString(16) : null;
            this.steps.push([log.getPC(), log.op.toString(), top, log.getDepth()]);
        },
        fault: function(log) {},
        result: function() { return this.steps; }
    }"""
}
FRAME_TRACER = {
    "tracer": """{
        frames: [],
        enter: function(frame) {
            this.frames.push([frame.getType(), toHex(frame.getTo()), toHex(frame.getInput())]);
        },
        exit: function(res) {},
        fault: function(log) {},
        result: function() { return this.frames; }
    }"""
}
# TODO: remove once the fork takes the evm2 batch-tracing fix.
BATCH_TRACING_UNFIXED = pytest.mark.skip(
    reason="tempo's evm2 traces only a batch's last call, and trace_replayTransaction fails on a batch"
)


def _vm_ops(vm_trace: dict | None) -> list[str | None]:
    """A Parity vmTrace's opcodes, each subcall's right after the op that made it; None for an entry without one."""
    ops = []
    for op in (vm_trace or {}).get("ops", []):
        ops += [op.get("op"), *_vm_ops(op.get("sub"))]
    return ops


class TestBatch:
    """Every call of a batch shows in each tracer."""

    @pytest.fixture
    async def batch(self, w3, chain_id, funded_account, deploy):
        """A create and a call, sent alone and then as one batch: the called address and the three tx hashes."""
        address = await deploy(RETURN_42_RUNTIME)
        create, call = {"to": None, "data": RETURN_42_INIT}, {"to": address, "data": b""}
        hashes = []
        for calls in ([create], [call], [create, call]):
            receipt = await send_calls(w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=calls)
            hashes.append(receipt["transactionHash"].to_0x_hex())
        return address, hashes

    @BATCH_TRACING_UNFIXED
    async def test_call_tracer(self, w3, funded_account, batch):
        """Each call is a frame, whether the tracer roots it or nests it."""
        address, (*_, both) = batch
        trace = await trace_tx(w3, both, CALL_TRACER)
        assert HexBytes(trace["from"]) == HexBytes(funded_account.address)
        frames = [(frame["type"], (frame.get("to") or "").lower()) for frame in call_frames(trace)]
        assert any(kind == "CREATE" for kind, _ in frames), frames
        assert ("CALL", address.lower()) in frames, frames

    @BATCH_TRACING_UNFIXED
    async def test_only_top_call_keeps_the_batch(self, w3, batch):
        """A batch's top calls are its own, so `onlyTopCall` keeps them and drops what they called."""
        address, (*_, both) = batch
        trace = await trace_tx(w3, both, CALL_TRACER | {"tracerConfig": {"onlyTopCall": True}})
        calls = trace.get("calls", [])
        assert [call["type"] for call in calls] == ["CREATE", "CALL"], trace
        assert calls[1]["to"].lower() == address.lower() and not any("calls" in call for call in calls)

    @BATCH_TRACING_UNFIXED
    async def test_call_tracer_marks_the_failing_call(self, w3, chain_id, funded_account, deploy):
        """A call that fails reverts its batch: it and the root carry the error, the call before it none."""
        ok, failing = [await deploy(runtime) for runtime in (RETURN_42_RUNTIME, SPIN_RUNTIME)]
        calls = [{"to": address, "data": b""} for address in (ok, failing)]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=calls)
        assert receipt["status"] == 0
        trace = await trace_tx(w3, receipt, CALL_TRACER)
        errors = {frame["to"].lower(): frame.get("error") for frame in call_frames(trace)[1:]}
        assert trace.get("error") and errors == {ok.lower(): None, failing.lower(): "out of gas"}, trace

    @BATCH_TRACING_UNFIXED
    async def test_struct_logs(self, w3, batch):
        traces = [await trace_tx(w3, tx_hash) for tx_hash in batch[1]]
        create, call, both = [[(log["op"], log["depth"]) for log in trace["structLogs"]] for trace in traces]
        assert create and call, "a single call's struct logs"
        assert both == create + call, "a batch's calls run at the depth of a call sent alone"

    @BATCH_TRACING_UNFIXED
    async def test_vm_trace(self, w3, batch):
        """A batch's root runs no opcode itself: it lists an entry without one per call, holding that call's."""
        _, hashes = batch
        replays = [await rpc_result(w3, "trace_replayTransaction", [tx_hash, ["vmTrace"]]) for tx_hash in hashes]
        create, call, both = [_vm_ops(replay["vmTrace"]) for replay in replays]
        # A call sent alone is the root itself, or the one entry of a root like the batch's.
        create, call = ([op for op in ops if op] for ops in (create, call))
        assert create and call, "a single call's vmTrace"
        assert both == [None, *create, None, *call]

    async def test_js_steps_match_the_struct_logs(self, w3, batch):
        """A JS step sees its struct log's pc, opcode, stack top and depth; a batch's steps are its calls' in turn."""
        _, (create, call, both) = batch
        alone = [
            [[log["pc"], log["op"], (log["stack"] or [None])[-1], log["depth"]] for log in trace["structLogs"]]
            for trace in [await trace_tx(w3, tx_hash) for tx_hash in (create, call)]
        ]
        for tx_hash, steps in zip((create, call, both), [*alone, alone[0] + alone[1]]):
            assert await trace_tx(w3, tx_hash, STEP_TRACER) == steps

    async def test_the_root_reports_its_tx(self, w3, funded_account, batch):
        """Alone or in a batch, the root carries the tx's sender, gas and outcome under both tracers."""
        for tx_hash in batch[1]:
            gas_used = (await w3.eth.get_transaction_receipt(tx_hash))["gasUsed"]
            root = await trace_tx(w3, tx_hash, CALL_TRACER)
            assert HexBytes(root["from"]) == HexBytes(funded_account.address)
            assert int(root["gasUsed"], 16) == gas_used and "error" not in root, root
            default = await trace_tx(w3, tx_hash)
            assert default["gas"] == gas_used and not default["failed"]

    async def test_a_self_destruct_is_traced_once(self, w3, chain_id, funded_account, deploy):
        """The root takes its last call's outcome, yet destroys nothing itself."""
        doomed = await deploy(DESTRUCT_RUNTIME)
        calls = [{"to": None, "data": RETURN_42_INIT}, {"to": doomed, "data": b""}]
        receipt = await send_calls(w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=calls)
        assert receipt["status"] == 1
        trace = await trace_tx(w3, receipt, CALL_TRACER)
        destroyed = [frame["from"].lower() for frame in call_frames(trace) if frame["type"] == "SELFDESTRUCT"]
        assert destroyed == [doomed.lower()], trace
        tx_hash = receipt["transactionHash"].to_0x_hex()
        replay = await rpc_result(w3, "trace_replayTransaction", [tx_hash, ["trace"]])
        suicides = [entry["action"]["address"].lower() for entry in replay["trace"] if entry["type"] == "suicide"]
        assert suicides == [doomed.lower()], replay


class TestForwardedCall:
    """A call a contract makes with calldata from its memory."""

    async def test_js_frames_match_the_call_tracer(self, w3, chain_id, funded_account, deploy, target):
        """A JS frame sees the call tracer's type, target and input; neither enters the tx's own call."""
        # Copy the calldata to memory, then call `target` with it.
        runtime = bytes.fromhex("365f5f375f5f365f5f73") + HexBytes(target) + bytes.fromhex("5af100")
        forwarder = await deploy(runtime)
        data = bytes.fromhex("deadbeef") + bytes(range(32))
        receipt = await send_call(w3, chain_id, funded_account, forwarder, data)
        trace = await trace_tx(w3, receipt, CALL_TRACER)
        # The tx's own call: the root, or its one call where the tracer roots a tx like a batch.
        sent = next(frame for frame in call_frames(trace) if frame["to"].lower() == forwarder.lower())
        frames = [[frame["type"], frame["to"].lower(), frame["input"]] for frame in call_frames(sent)[1:]]
        assert frames == [["CALL", target.lower(), "0x" + data.hex()]], trace
        assert await trace_tx(w3, receipt, FRAME_TRACER) == frames


class TestTransfer:
    """A TIP-20 transfer's trace."""

    @pytest.fixture
    async def receipt(self, w3, chain_id, funded_account):
        return await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))

    async def test_struct_logger_responds(self, w3, receipt):
        trace = await trace_tx(w3, receipt)
        assert {"gas", "failed", "returnValue", "structLogs"} <= set(trace.keys())
        assert isinstance(trace["structLogs"], list)

    async def test_its_block_traces_it(self, w3, receipt):
        block = hex(receipt["blockNumber"])
        traces = await rpc_result(w3, "debug_traceBlockByNumber", [block, CALL_TRACER])
        assert receipt["transactionHash"].to_0x_hex() in [trace["txHash"] for trace in traces]
