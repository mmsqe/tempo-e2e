"""Chain identity: what the node reports, and what it keys off the id."""

import re
import socket
import threading
import time

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import AsyncWeb3
from web3.exceptions import Web3RPCError

from .network import DEAD_UPSTREAM, FollowerNode, dev_node, free_port, generate_dev_genesis, node_log
from .utils import fund, new_account, send_call, transfer_call

# An id no binary claims -- upstream keys chains off 4217/42431.
UNCLAIMED_CHAIN_ID = 424242
DEVNET_CHAIN_ID = 787222

BOOT_PING = re.compile(r"pinging boot node record=NodeRecord \{ address: ([^,]+)")


def _record_connects(proxy: socket.socket, hosts: list[str]) -> None:
    """Note the host of each CONNECT to ``proxy``, then hang up."""
    while True:
        try:
            conn, _ = proxy.accept()
        except OSError:
            return
        with conn:
            request = conn.recv(4096).decode(errors="replace").split()
            if request[:1] == ["CONNECT"]:
                hosts.append(request[1].rsplit(":", 1)[0])


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

            signer = new_account()
            await fund(w3, signer.address)
            recipient = new_account().address
            transfer = transfer_call(recipient, 1)
            await send_call(w3, UNCLAIMED_CHAIN_ID, signer, transfer["to"], transfer["data"])
            assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1

            # Signed one id over, the same write has to bounce.
            with pytest.raises(Web3RPCError, match="chain ID"):
                await send_call(w3, UNCLAIMED_CHAIN_ID + 1, signer, transfer["to"], transfer["data"])
        finally:
            node.stop()


@pytest.mark.slow
class TestBootnodes:
    """No id falls back to upstream's or Ethereum's peers."""

    @pytest.fixture
    def lookups(self, tmp_path, monkeypatch):
        """Run a follower with discovery on; return the bootnodes it pinged and the hosts it fetched."""
        proxy = socket.create_server(("127.0.0.1", 0))
        fetched: list[str] = []
        threading.Thread(target=_record_connects, args=(proxy, fetched), daemon=True).start()
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.getsockname()[1]}")
        monkeypatch.setenv("RUST_LOG", "info,discv4=debug")

        def run(chain_id: int, *args: str) -> dict:
            node = FollowerNode(
                upstream=DEAD_UPSTREAM,
                discovery=True,
                genesis=generate_dev_genesis(tmp_path / "genesis", chain_id=chain_id, dkg=True),
                datadir=tmp_path / "node",
                log_path=tmp_path / "node.log",
                http_port=free_port(),
                extra_args=["--nat", "none", *args],  # no public-ip lookups through the proxy
            )
            try:
                node.start().wait_for_rpc(timeout=60, want_block=0)
                time.sleep(3)  # discv4 bootstraps and the endpoint is fetched just after start
            finally:
                node.stop()
            return {"pinged": sorted(set(BOOT_PING.findall(node_log(node)))), "fetched": fetched}

        yield run
        proxy.close()

    @pytest.mark.parametrize("chain_id", [DEVNET_CHAIN_ID, UNCLAIMED_CHAIN_ID])
    def test_looks_up_no_peers_by_default(self, lookups, chain_id):
        assert lookups(chain_id) == {"pinged": [], "fetched": []}

    def test_looks_up_the_peers_it_is_given(self, lookups):
        enode = f"enode://{'ab' * 64}@127.0.0.1:{free_port()}"
        endpoint = "https://bootnodes.invalid/"
        found = lookups(UNCLAIMED_CHAIN_ID, "--bootnodes", enode, "--tempo.bootnodes-endpoint", endpoint)
        assert found == {"pinged": ["127.0.0.1"], "fetched": ["bootnodes.invalid"]}
