import pytest
from web3 import Web3

from .network import dev_node, running
from .utils import (
    DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
    PLAIN_CALL_GAS,
    RETURN_42_RUNTIME,
    funded,
    new_account,
    rpc,
    send_call,
    sign_type_2,
    transfer_call,
)

pytestmark = pytest.mark.tempo

TIP = DEFAULT_MAX_PRIORITY_FEE_PER_GAS


async def test_client_version_is_tempo(w3):
    assert "tempo" in (await w3.client_version).lower()


async def test_node_is_not_syncing(w3):
    assert (await w3.eth.syncing) is False


async def test_get_transaction_by_hash(w3, chain_id, funded_account):
    receipt = await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))
    tx = await w3.eth.get_transaction(receipt["transactionHash"])
    assert tx["hash"] == receipt["transactionHash"]
    assert tx["type"] == 0x76  # the native tempo AA transaction type


async def test_block_receipts_include_the_transaction(w3, chain_id, funded_account):
    receipt = await send_call(w3, chain_id, funded_account, **transfer_call(new_account().address, 1))
    receipts = await w3.eth.get_block_receipts(receipt["blockNumber"])
    assert any(r["transactionHash"] == receipt["transactionHash"] for r in receipts)


async def test_call_with_state_override(w3):
    # Inject the RETURN(42) runtime at a fresh address for this call only; no deploy needed.
    addr = Web3.to_checksum_address("0x" + "11" * 20)
    override = {addr: {"code": "0x" + RETURN_42_RUNTIME.hex()}}
    result = await w3.eth.call({"to": addr}, "latest", override)
    assert int(result.hex(), 16) == 42


async def test_dev_blocks_have_no_consensus_context(w3):
    """TIP-1031: only consensus-produced blocks set the optional consensusContext header field."""
    block = await w3.eth.get_block("latest")
    assert block.get("consensusContext") is None


@pytest.mark.slow
@pytest.mark.parametrize(
    ("flags", "suggested"), [((), 0), (("--gpo.ignoreprice", "1"), TIP)], ids=["default", "ignoreprice"]
)
async def test_priority_fee_suggestion_samples_zero_tips(tmp_path, flags, suggested):
    """The gas oracle counts zero-tip transactions unless ``--gpo.ignoreprice`` skips them (#8329). One
    block holds three zero-tip calls and a tipped one; ``--gpo.blocks 1`` samples that block alone, and
    five-second blocks keep it the latest while the oracle is asked."""
    with running(dev_node(tmp_path, block_time="5s", extra_args=["--gpo.blocks", "1", *flags])) as node:
        async with rpc(node) as w3:
            payer = await funded(w3)
            nonce = await w3.eth.get_transaction_count(payer.address)
            raws = [
                await sign_type_2(
                    w3, payer, payer.address, gas=PLAIN_CALL_GAS, nonce=nonce + i, max_priority_fee_per_gas=tip
                )
                for i, tip in enumerate((0, 0, 0, TIP))
            ]
            hashes = [await w3.eth.send_raw_transaction(raw) for raw in raws]
            blocks = {(await w3.eth.wait_for_transaction_receipt(h))["blockNumber"] for h in hashes}
            assert len(blocks) == 1, "the four calls split across blocks"

            fee = await w3.eth.max_priority_fee
            assert await w3.eth.block_number in blocks, "a block landed before the oracle answered"
            assert fee == suggested
