"""From T12 the registry takes as a validator's fee recipient only the router the genesis' fee
router factory holds for it, and a block pays its proposer's recipient.

Needs ``--consensus`` and a tempo built from ``staking``."""

import pytest
from eth_account import Account
from eth_contract.create3 import create3_address
from tempo.constants import PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS

from .abi import FEE_ROUTER_FACTORY, VALIDATOR_CONFIG_V2
from .conftest import LOCALNET_CHAIN_ID, localnet
from .network import FAUCET_PRIVATE_KEY
from .staking import STAKING_SALT, create3, create3_salt, created_routers, deploy, factory_initcode, lockbox_initcode
from .utils import call_revert, connect, cs, new_account, wait_for_block

pytestmark = pytest.mark.tempo

N_VALIDATORS = 3
FAUCET = Account.from_key(FAUCET_PRIVATE_KEY)
# The genesis names the factory before it exists: the faucet's, under this salt.
FACTORY_SALT = create3_salt(FAUCET.address, b"factory")
FACTORY_GENESIS_ADDRESS = create3_address(FACTORY_SALT, deployer=FAUCET.address)


@pytest.fixture(scope="module")
def routed_net(request, tmp_path_factory):
    """Three validators past T12 from genesis, which names the factory's address."""
    genesis = {"t12Time": 0, "feeRouterFactory": FACTORY_GENESIS_ADDRESS}
    yield from localnet(request, tmp_path_factory, "fee-recipient", N_VALIDATORS, epoch_length=20, genesis=genesis)


@pytest.fixture
async def w3(routed_net):
    client = connect(routed_net.node_rpc_url("node0"))
    yield client
    await client.provider.disconnect()


@pytest.mark.consensus
@pytest.mark.slow
async def test_a_recipient_must_be_the_validators_router(w3):
    """Until the factory holds a router for a seat it takes no recipient; then only that router,
    which the seat's next blocks pay."""
    registry = VALIDATOR_CONFIG_V2_ADDRESS
    first, second, *_ = await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry)
    validator, idx = cs(first[1]), first[5]

    async def refused(recipient):
        data = VALIDATOR_CONFIG_V2.fns.setFeeRecipient(idx, recipient).data
        return "InvalidValidatorAddress" in await call_revert(w3, registry, data, sender=FAUCET.address)

    assert await refused(new_account().address), "no factory yet, nothing is taken"

    staking = await deploy(w3, LOCALNET_CHAIN_ID, FAUCET, PATH_USD, salt=STAKING_SALT)
    lockbox = await create3(w3, LOCALNET_CHAIN_ID, FAUCET, lockbox_initcode(FAUCET.address))
    initcode = factory_initcode(staking.address, lockbox, FAUCET.address, new_account().address)
    factory = await create3(w3, LOCALNET_CHAIN_ID, FAUCET, initcode, FACTORY_SALT)
    assert factory == FACTORY_GENESIS_ADDRESS, "factory != genesis feeRouterFactory address"
    assert await refused(validator), "its own address is not its router"

    async def create(for_validator):
        receipt = await staking.send(FAUCET, FEE_ROUTER_FACTORY.fns.create(for_validator, operator, 1_000), to=factory)
        return created_routers(receipt)[0]

    operator = new_account().address
    router, other = await create(validator), await create(cs(second[1]))
    assert cs(await FEE_ROUTER_FACTORY.fns.routerOf(validator).call(w3, to=factory)) == router
    assert await refused(other), "another seat's router is still refused"

    receipt = await staking.send(FAUCET, VALIDATOR_CONFIG_V2.fns.setFeeRecipient(idx, router), to=registry)
    [entry] = [v for v in await VALIDATOR_CONFIG_V2.fns.getActiveValidators().call(w3, to=registry) if v[5] == idx]
    assert cs(entry[4]) == router

    # From the next block on, the seat's blocks name the router as their beneficiary.
    start = receipt["blockNumber"] + 1
    last = await wait_for_block(w3, start + 3 * N_VALIDATORS)
    miners = {cs((await w3.eth.get_block(n))["miner"]) for n in range(start, last + 1)}
    assert router in miners, miners
    assert validator not in miners, miners
