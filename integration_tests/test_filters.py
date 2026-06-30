"""Event logs and filters."""

import asyncio

from tempo.constants import PATH_USD

from .utils import TRANSFER_TOPIC, new_account, send_calls, transfer_call


async def test_transfer_emits_transfer_log(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 5)]
    )
    block = receipt["blockNumber"]
    logs = await w3.eth.get_logs({"fromBlock": block, "toBlock": block, "address": PATH_USD})
    assert any(log["topics"][0] == TRANSFER_TOPIC for log in logs)


async def test_get_logs_over_block_range(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    first = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=[transfer_call(new_account().address, 1)])
    second = await send_calls(w3, chain_id=chain_id, private_key=pk, calls=[transfer_call(new_account().address, 2)])
    logs = await w3.eth.get_logs(
        {
            "fromBlock": first["blockNumber"],
            "toBlock": second["blockNumber"],
            "address": PATH_USD,
            "topics": [TRANSFER_TOPIC],
        }
    )
    blocks = {log["blockNumber"] for log in logs}
    assert first["blockNumber"] in blocks and second["blockNumber"] in blocks


async def test_block_filter_reports_new_blocks(w3):
    block_filter = await w3.eth.filter("latest")
    await asyncio.sleep(2.5)  # dev block time is 1s
    changes = await w3.eth.get_filter_changes(block_filter.filter_id)
    assert len(changes) >= 1


async def test_log_filter_captures_transfer(w3, chain_id, funded_account):
    log_filter = await w3.eth.filter({"address": PATH_USD, "topics": [TRANSFER_TOPIC]})
    await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 7)]
    )
    changes = await w3.eth.get_filter_changes(log_filter.filter_id)
    assert len(changes) >= 1
    assert changes[0]["topics"][0] == TRANSFER_TOPIC
