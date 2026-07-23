import pytest
from web3 import Web3

from .utils import call_revert, wait_for_block

pytestmark = pytest.mark.tempo

# Canonical EIP-2935 history-storage predeploy (alloy HISTORY_STORAGE_ADDRESS / _CODE).
HISTORY_STORAGE_ADDRESS = Web3.to_checksum_address("0x0000F90827F1C53a10cb7A02335B175320002935")
HISTORY_STORAGE_CODE = bytes.fromhex(
    "3373fffffffffffffffffffffffffffffffffffffffe14604657602036036042575f35"
    "600143038111604257611fff81430311604257611fff9006545f5260205ff35b5f5ffd5b"
    "5f35611fff60014303065500"
)


class TestHistoryStorage:
    async def test_predeployed(self, w3):
        assert bytes(await w3.eth.get_code(HISTORY_STORAGE_ADDRESS)) == HISTORY_STORAGE_CODE
        assert await w3.eth.get_transaction_count(HISTORY_STORAGE_ADDRESS) == 1

    async def test_returns_block_hash(self, w3):
        head = await wait_for_block(w3, 2)  # need at least one written parent (head-1)
        number = head - 1
        data = "0x" + number.to_bytes(32, "big").hex()
        stored = bytes(await w3.eth.call({"to": HISTORY_STORAGE_ADDRESS, "data": data}))
        canonical = bytes((await w3.eth.get_block(number))["hash"])
        assert stored == canonical, f"history store for block {number} != canonical hash"

    async def test_rejects_out_of_window(self, w3):
        head = await w3.eth.block_number
        # Far in the future: fails `number <= block.number - 1` regardless of the eth_call block env.
        data = "0x" + (head + 1000).to_bytes(32, "big").hex()
        await call_revert(w3, HISTORY_STORAGE_ADDRESS, data)
