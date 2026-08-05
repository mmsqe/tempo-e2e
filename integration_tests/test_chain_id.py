"""Chain identity: what the node reports, and ids no binary has an entry for."""

import pytest
from web3 import AsyncWeb3
from web3.exceptions import Web3RPCError

from .abi import ANCHORING, ANCHORING_ADDRESS
from .network import dev_node
from .utils import fund, new_account, send_call

# An id no binary claims -- upstream keys chains off 4217/42431, this fork off 787222.
UNCLAIMED_CHAIN_ID = 424242


class TestReportedIdentity:
    async def test_chain_id_is_positive(self, chain_id):
        assert chain_id > 0

    async def test_eth_chain_id_matches(self, w3, chain_id):
        assert await w3.eth.chain_id == chain_id

    async def test_net_version_matches_chain_id(self, w3, chain_id):
        assert int(await w3.net.version) == chain_id


@pytest.mark.tempo
@pytest.mark.slow
class TestUnclaimedChainId:
    async def test_runs_from_genesis_alone(self, tmp_path):
        """Editing ``chainId`` in genesis is enough: nothing the node needs is keyed off it."""
        node = dev_node(tmp_path, log_name="unclaimed-chain-id.log", chain_id=UNCLAIMED_CHAIN_ID)
        try:
            node.start().wait_for_rpc()
            w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(node.rpc_url))
            assert await w3.eth.chain_id == UNCLAIMED_CHAIN_ID
            assert int(await w3.net.version) == UNCLAIMED_CHAIN_ID
            assert await w3.eth.get_code(ANCHORING_ADDRESS) == b"\xef", "T10 activated from the genesis config"

            signer = new_account()
            await fund(w3, signer.address)
            key, commitment = b"\x11" * 32, b"\x22" * 32
            anchor = ANCHORING.fns.anchor(key, commitment, b"").data
            await send_call(w3, UNCLAIMED_CHAIN_ID, signer, ANCHORING_ADDRESS, anchor)
            read = ANCHORING.fns.latest(signer.address, key)
            assert bytes(await read.call(w3, to=ANCHORING_ADDRESS)) == commitment

            # Signed one id over, the same write has to bounce.
            with pytest.raises(Web3RPCError, match="chain ID"):
                await send_call(w3, UNCLAIMED_CHAIN_ID + 1, signer, ANCHORING_ADDRESS, anchor)
        finally:
            node.stop()
