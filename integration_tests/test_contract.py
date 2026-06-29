"""EVM contract deployment and calls via tempo (0x76) transactions."""

from .utils import deploy_contract, get_nonce

# Init code that deploys runtime returning the constant 42.
RETURN_42_INIT = "600a600c600039600a6000f3602a60005260206000f3"
RETURN_42_RUNTIME = bytes.fromhex("602a60005260206000f3")


async def test_deploy_sets_code(w3, chain_id, funded_account):
    receipt, address = await deploy_contract(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), bytecode=RETURN_42_INIT
    )
    assert receipt["status"] == 1
    assert address is not None
    assert await w3.eth.get_code(address) == RETURN_42_RUNTIME


async def test_deployed_contract_is_callable(w3, chain_id, funded_account):
    _, address = await deploy_contract(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), bytecode=RETURN_42_INIT
    )
    assert int((await w3.eth.call({"to": address, "data": "0x"})).hex(), 16) == 42


async def test_sequential_deploys_get_distinct_addresses(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    nonce = await get_nonce(w3, funded_account.address)
    _, addr1 = await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=RETURN_42_INIT, nonce=nonce)
    _, addr2 = await deploy_contract(w3, chain_id=chain_id, private_key=pk, bytecode=RETURN_42_INIT, nonce=nonce + 1)
    assert addr1 != addr2
