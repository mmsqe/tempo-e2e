"""debug_ and trace_ tracing of tempo txs."""

import asyncio

import pytest
from eth_contract.erc20 import ERC20
from hexbytes import HexBytes

from .utils import (
    CALL_TRACER,
    DESTRUCT_RUNTIME,
    RETURN_42_INIT,
    RETURN_42_RUNTIME,
    SPIN_RUNTIME,
    STABLECOINS,
    TRANSFER_TOPIC,
    call_frames,
    gas_cost_in_token,
    new_account,
    rpc_result,
    send_call,
    send_calls,
    send_type_2,
    sign_type_2,
    suggested_max_fee,
    trace_tx,
    transfer_call,
)

pytestmark = pytest.mark.tempo

STORE_RUNTIME = bytes.fromhex("602a5f5500")  # PUSH1 42 PUSH0 SSTORE STOP
REVERT_RUNTIME = bytes.fromhex("602a5f5260205ffd")  # reverts with the word 42
WORD_42 = f"0x{42:064x}"
PRESTATE_TRACER = {"tracer": "prestateTracer"}
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
# TODO: remove both once the fork takes the evm2 batch-tracing fix.
BATCH_TRACING_UNFIXED = pytest.mark.skip(
    reason="tempo's evm2 traces only a batch's last call, and trace_replayTransaction fails on a batch"
)
WITH_LOG_UNFIXED = pytest.mark.skip(reason="tempo's evm2 fails callTracer withLog on a TIP-20 transfer")


def _vm_ops(vm_trace: dict | None) -> list[str | None]:
    """A Parity vmTrace's opcodes, each subcall's right after the op that made it; None for an entry without one."""
    ops = []
    for op in (vm_trace or {}).get("ops", []):
        ops += [op.get("op"), *_vm_ops(op.get("sub"))]
    return ops


async def _trace_call(w3, call: dict, config=None, block="latest"):
    """``call``'s ``debug_traceCall`` on ``block``."""
    return await rpc_result(w3, "debug_traceCall", [call, block, config or {}])


async def _sent(w3, account, to, data=b"", **kwargs):
    """A plain tx's receipt, whatever its status, and its call trace, which must report the receipt's gas."""
    receipt = await send_type_2(w3, account, to, data, check=False, **kwargs)
    trace = await trace_tx(w3, receipt, CALL_TRACER)
    assert int(trace["gasUsed"], 16) == receipt["gasUsed"], trace
    return receipt, trace


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


class TestTx:
    """A tx of one call, traced as geth traces one."""

    async def test_a_call_out_of_gas(self, w3, funded_account, deploy):
        spin = await deploy(SPIN_RUNTIME)
        receipt, trace = await _sent(w3, funded_account, spin, gas=400_000)
        assert receipt["status"] == 0 and trace["error"] == "out of gas", trace
        assert receipt["gasUsed"] == 400_000

    async def test_a_creation_out_of_gas(self, w3, funded_account):
        """With gas to run its init code but not to store what it returns, a creation fails storing it."""
        empty, big = (bytes.fromhex(f"61{size:04x}5ff3") for size in (0, 4096))  # return `size` zero bytes
        sender = funded_account.address
        empty_gas, big_gas = [await w3.eth.estimate_gas({"from": sender, "data": code}) for code in (empty, big)]
        receipt, trace = await _sent(w3, funded_account, None, big, gas=(empty_gas + big_gas) // 2)
        # geth says "contract creation code storage out of gas", evm2 only "out of gas".
        assert receipt["status"] == 0 and "out of gas" in trace["error"], trace
        steps = [(log["op"], log.get("error")) for log in (await trace_tx(w3, receipt))["structLogs"]]
        assert steps == [("PUSH2", None), ("PUSH0", None), ("RETURN", None)], steps  # the init code ran to the end

    @pytest.fixture
    async def transfer(self, w3, chain_id, funded_account):
        """The receipt of a TIP-20 transfer."""
        return await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))

    async def test_every_tracer_at_once(self, w3, transfer):
        """Each tracer traces a TIP-20 transfer, and its block's traces list it."""
        configs = [
            {},
            CALL_TRACER,
            CALL_TRACER | {"tracerConfig": {"onlyTopCall": True}},
            CALL_TRACER | {"tracerConfig": {"diffMode": True}},  # not a callTracer option: ignored
        ]
        default, call, _, in_diff_mode = await asyncio.gather(*(trace_tx(w3, transfer, c) for c in configs))
        assert not default["failed"] and in_diff_mode == call
        traces = await rpc_result(w3, "debug_traceBlockByNumber", [hex(transfer["blockNumber"]), CALL_TRACER])
        assert transfer["transactionHash"].to_0x_hex() in [trace["txHash"] for trace in traces]

    @WITH_LOG_UNFIXED
    async def test_with_log(self, w3, transfer):
        """`withLog` shows the transfer's Transfer log."""
        trace = await trace_tx(w3, transfer, CALL_TRACER | {"tracerConfig": {"withLog": True}})
        topics = [log["topics"][0] for frame in call_frames(trace) for log in frame.get("logs", [])]
        assert TRANSFER_TOPIC.to_0x_hex() in topics, trace

    async def test_a_staticcall(self, w3, funded_account, deploy, target):
        """A STATICCALL is a frame of its own, with or without an access list naming its target."""
        # Return the first word `target` returns to a STATICCALL.
        runtime = bytes.fromhex("60205f5f5f73") + HexBytes(target) + bytes.fromhex("5afa5060205ff3")
        caller = await deploy(runtime)
        for access_list in ([], [{"address": target, "storageKeys": []}]):
            receipt, trace = await _sent(w3, funded_account, caller, gas=1_000_000, access_list=access_list)
            calls = [(call["type"], call["to"].lower(), call["output"]) for call in trace["calls"]]
            assert receipt["status"] == 1 and calls == [("STATICCALL", target.lower(), WORD_42)], trace

    async def test_txs_sent_together(self, w3, funded_account, deploy):
        """Each of three txs sent at once traces on the state the ones before it left."""
        doomed = [await deploy(DESTRUCT_RUNTIME) for _ in range(3)]
        nonce = await w3.eth.get_transaction_count(funded_account.address)
        raws = [
            await sign_type_2(w3, funded_account, to, gas=1_000_000, nonce=nonce + i) for i, to in enumerate(doomed)
        ]
        for tx_hash in [await w3.eth.send_raw_transaction(raw) for raw in raws]:
            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
            trace = await trace_tx(w3, receipt, CALL_TRACER)
            assert receipt["status"] == 1 and "error" not in trace, trace
            assert [frame["type"] for frame in call_frames(trace)] == ["CALL", "SELFDESTRUCT"], trace
            assert int(trace["gasUsed"], 16) == receipt["gasUsed"], trace

    async def test_a_revert_refunds_unused_gas(self, w3, funded_account, deploy):
        """A reverted tx pays for the gas it used, which its trace reports with the revert data."""
        reverter = await deploy(REVERT_RUNTIME)

        async def holdings():
            return sum([await ERC20.fns.balanceOf(funded_account.address).call(w3, to=t) for t in STABLECOINS.values()])

        before = await holdings()
        receipt, trace = await _sent(w3, funded_account, reverter, gas=1_000_000)
        assert receipt["status"] == 0 and receipt["gasUsed"] < 1_000_000
        assert before - await holdings() == gas_cost_in_token(receipt)
        assert (trace["error"], trace["output"]) == ("execution reverted", WORD_42), trace

    async def test_js_frames_match_the_call_tracer(self, w3, chain_id, funded_account, deploy, target):
        """A JS frame sees the type, target and input the call tracer shows for a call made from memory."""
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


class TestTraceCall:
    """debug_traceCall, traced as geth traces it."""

    async def test_struct_logs(self, w3, funded_account, target):
        """With a gas limit or without, a call logs the opcodes it runs."""
        call = {"from": funded_account.address, "to": target}
        for tx in (call | {"gas": hex(100_000)}, call):
            trace = await _trace_call(w3, tx)
            assert (trace["failed"], trace["returnValue"].removeprefix("0x")) == (False, WORD_42[2:]), trace
            assert [log["op"] for log in trace["structLogs"]] == "PUSH1 PUSH1 MSTORE PUSH1 PUSH1 RETURN".split()

    async def test_call_tracer(self, w3, funded_account, target):
        """The call's frame, with the gas limit it is given, or the node's without one."""
        call = {"from": funded_account.address, "to": target}
        limited, unlimited = [await _trace_call(w3, tx, CALL_TRACER) for tx in (call | {"gas": hex(100_000)}, call)]
        frame = {"type": "CALL", "from": call["from"].lower(), "to": target.lower(), "input": "0x", "output": WORD_42}
        for trace in (limited, unlimited):
            assert {key: trace[key] for key in frame} == frame, trace
        assert limited["gas"] == hex(100_000) and limited["gasUsed"] == unlimited["gasUsed"], (limited, unlimited)

    async def test_prestate_tracer(self, w3, funded_account, target):
        """The sender's nonce and the target's code, before the call."""
        sender = funded_account.address
        result = await _trace_call(w3, {"from": sender, "to": target}, PRESTATE_TRACER)
        assert result[sender.lower()]["nonce"] == await w3.eth.get_transaction_count(sender), result
        assert result[target.lower()]["code"] == "0x" + RETURN_42_RUNTIME.hex(), result

    async def test_prestate_diff(self, w3, funded_account, deploy, target):
        """On an earlier block, the sender's nonce then, bumped, and what the call stores."""
        sender = funded_account.address
        store = await deploy(STORE_RUNTIME)
        receipts = [await send_type_2(w3, funded_account, target, b"") for _ in range(3)]
        block = receipts[1]["blockNumber"]  # two of the three in
        config = PRESTATE_TRACER | {"tracerConfig": {"diffMode": True}}
        diff = await _trace_call(w3, {"from": sender, "to": store}, config, hex(block))
        nonce = await w3.eth.get_transaction_count(sender, block)
        sender, store = sender.lower(), store.lower()
        assert (diff["pre"][sender]["nonce"], diff["post"][sender]["nonce"]) == (nonce, nonce + 1), diff
        assert diff["post"][store]["storage"] == {f"0x{0:064x}": WORD_42}, diff

    async def test_state_overrides(self, w3):
        """The call runs the code, and its sender has the balance and nonce, that the overrides give."""
        sender, target = new_account().address, new_account().address
        state = {"balance": hex(10**18), "nonce": 7}
        overrides = {sender: state | {"nonce": hex(7)}, target: {"code": "0x" + RETURN_42_RUNTIME.hex()}}
        call = {"from": sender, "to": target}
        trace = await _trace_call(w3, call, {"stateOverrides": overrides})
        assert trace["returnValue"].removeprefix("0x") == WORD_42[2:], trace
        config = PRESTATE_TRACER | {"stateOverrides": overrides}
        result = await _trace_call(w3, call, config)
        assert result[sender.lower()] == state, result

    async def test_revert_data(self, w3, funded_account, deploy):
        reverter = await deploy(REVERT_RUNTIME)
        call = {"from": funded_account.address, "to": reverter}
        trace = await _trace_call(w3, call)
        assert (trace["failed"], trace["returnValue"].removeprefix("0x")) == (True, WORD_42[2:]), trace

    async def test_block_overrides(self, w3, funded_account):
        """NUMBER reads the block number the call is given."""
        call = {"from": funded_account.address, "input": "0x43"}  # NUMBER, as init code
        trace = await _trace_call(w3, call, {"blockOverrides": {"number": "0x1337"}})
        steps = [(log["op"], log.get("stack")) for log in trace["structLogs"]]
        assert steps == [("NUMBER", []), ("STOP", ["0x1337"])], trace

    async def test_a_sender_without_funds(self, w3, target):
        """A sender with no fee token can't pay for a call that names a fee."""
        call = {"from": new_account().address, "to": target, "gas": hex(1_000_000)}  # TIP-1000's first-tx cost
        call["maxFeePerGas"] = hex(await suggested_max_fee(w3))
        for tracer in (PRESTATE_TRACER, CALL_TRACER):
            resp = await w3.provider.make_request("debug_traceCall", [call, "latest", tracer])
            assert "insufficient" in str(resp.get("error")), resp
