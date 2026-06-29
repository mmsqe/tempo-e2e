"""Chain identity: eth_chainId / net_version consistency."""


async def test_chain_id_is_positive(chain_id):
    assert chain_id > 0


async def test_eth_chain_id_matches(w3, chain_id):
    assert await w3.eth.chain_id == chain_id


async def test_net_version_matches_chain_id(w3, chain_id):
    assert int(await w3.net.version) == chain_id
