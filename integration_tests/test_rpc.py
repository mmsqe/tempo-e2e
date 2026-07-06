from web3 import Web3

from .utils import RETURN_42_RUNTIME, new_account, send_calls, transfer_call


async def test_client_version_is_tempo(w3):
    assert "tempo" in (await w3.client_version).lower()


async def test_node_is_not_syncing(w3):
    assert (await w3.eth.syncing) is False


async def test_get_transaction_by_hash(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 1)]
    )
    tx = await w3.eth.get_transaction(receipt["transactionHash"])
    assert tx["hash"] == receipt["transactionHash"]
    assert tx["type"] == 0x76  # the native tempo AA transaction type


async def test_block_receipts_include_the_transaction(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(new_account().address, 1)]
    )
    receipts = await w3.eth.get_block_receipts(receipt["blockNumber"])
    assert any(r["transactionHash"] == receipt["transactionHash"] for r in receipts)


async def test_call_with_state_override(w3):
    # Inject the RETURN(42) runtime at a fresh address for this call only; no deploy needed.
    addr = Web3.to_checksum_address("0x" + "11" * 20)
    override = {addr: {"code": "0x" + RETURN_42_RUNTIME.hex()}}
    result = await w3.eth.call({"to": addr}, "latest", override)
    assert int(result.hex(), 16) == 42


async def test_dev_blocks_have_no_consensus_context(w3):
    """TIP-1031: the consensusContext header field is optional and only set by
    consensus-produced blocks -- a dev-mode block omits it entirely."""
    block = await w3.eth.get_block("latest")
    assert block.get("consensusContext") is None
