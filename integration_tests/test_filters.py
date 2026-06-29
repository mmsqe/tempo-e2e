"""Event logs and filters."""

import asyncio

from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo.constants import PATH_USD

from .utils import new_account, send_calls

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = HexBytes("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")


async def test_transfer_emits_transfer_log(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 5).data}],
    )
    block = receipt["blockNumber"]
    logs = await w3.eth.get_logs({"fromBlock": block, "toBlock": block, "address": PATH_USD})
    assert any(log["topics"][0] == TRANSFER_TOPIC for log in logs)


async def test_block_filter_reports_new_blocks(w3):
    block_filter = await w3.eth.filter("latest")
    await asyncio.sleep(2.5)  # dev block time is 1s
    changes = await w3.eth.get_filter_changes(block_filter.filter_id)
    assert len(changes) >= 1


async def test_log_filter_captures_transfer(w3, chain_id, funded_account):
    log_filter = await w3.eth.filter({"address": PATH_USD, "topics": [TRANSFER_TOPIC]})
    await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 7).data}],
    )
    changes = await w3.eth.get_filter_changes(log_filter.filter_id)
    assert len(changes) >= 1
    assert changes[0]["topics"][0] == TRANSFER_TOPIC
