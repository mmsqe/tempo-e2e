import pytest
from tempo.constants import (
    ADDRESS_REGISTRY_ADDRESS,
    ALPHA_USD,
    FEE_MANAGER_ADDRESS,
    NONCE_ADDRESS,
    PATH_USD,
    STABLECOIN_DEX_ADDRESS,
    STORAGE_CREDITS_ADDRESS,
    TIP20_CHANNEL_RESERVE_ADDRESS,
    TIP20_FACTORY_ADDRESS,
    TIP403_REGISTRY_ADDRESS,
    VALIDATOR_CONFIG_ADDRESS,
    VALIDATOR_CONFIG_V2_ADDRESS,
)

from .abi import (
    ADDRESS_REGISTRY,
    DEX,
    FEE,
    NONCE,
    STORAGE_CREDITS,
    TIP20_CHANNEL_RESERVE,
    TIP20_FACTORY,
    TIP403,
    VALIDATOR_CONFIG,
)
from .utils import deploy_contract, init_code

pytestmark = pytest.mark.tempo

# Standard EVM identity precompile: echoes its input unchanged.
IDENTITY_PRECOMPILE = "0x0000000000000000000000000000000000000004"

# Forwards its calldata to {addr}, returning 32 bytes: calldatacopy, staticcall, pop the flag, return.
FORWARDER_RUNTIME = "36 6000 6000 37  6020 6000 36 6000 73{addr} 5a fa 50  6020 6000 f3"

# Reused / long calldatas (kept short for the case table below).
_VALIDATOR_COUNT = bytes(VALIDATOR_CONFIG.fns.validatorCount().data)
_IS_VIRTUAL = bytes(ADDRESS_REGISTRY.fns.isVirtualAddress(PATH_USD).data)
_DOMAIN_SEPARATOR = bytes(TIP20_CHANNEL_RESERVE.fns.domainSeparator().data)

# (label, address, calldata): a one-word pure/view getter per enshrined precompile; identity is the EVM baseline.
_PRECOMPILE_CASES = [
    ("identity", IDENTITY_PRECOMPILE, bytes(range(32))),
    ("FeeManager.getPoolId", FEE_MANAGER_ADDRESS, bytes(FEE.fns.getPoolId(ALPHA_USD, PATH_USD).data)),
    ("DEX.pairKey", STABLECOIN_DEX_ADDRESS, bytes(DEX.fns.pairKey(ALPHA_USD, PATH_USD).data)),
    ("TIP20Factory.isTIP20", TIP20_FACTORY_ADDRESS, bytes(TIP20_FACTORY.fns.isTIP20(PATH_USD).data)),
    ("Nonce.getNonce", NONCE_ADDRESS, bytes(NONCE.fns.getNonce(PATH_USD, 1).data)),
    ("TIP403.policyIdCounter", TIP403_REGISTRY_ADDRESS, bytes(TIP403.fns.policyIdCounter().data)),
    ("ValidatorConfig.validatorCount", VALIDATOR_CONFIG_ADDRESS, _VALIDATOR_COUNT),
    ("ValidatorConfigV2.validatorCount", VALIDATOR_CONFIG_V2_ADDRESS, _VALIDATOR_COUNT),
    ("AddressRegistry.isVirtualAddress", ADDRESS_REGISTRY_ADDRESS, _IS_VIRTUAL),
    ("StorageCredits.balanceOf", STORAGE_CREDITS_ADDRESS, bytes(STORAGE_CREDITS.fns.balanceOf(PATH_USD).data)),
    ("ChannelReserve.domainSeparator", TIP20_CHANNEL_RESERVE_ADDRESS, _DOMAIN_SEPARATOR),
]


@pytest.mark.parametrize("label,address,calldata", _PRECOMPILE_CASES, ids=[c[0] for c in _PRECOMPILE_CASES])
async def test_precompile_callable_from_contract(w3, chain_id, funded_account, label, address, calldata):
    """A contract's STATICCALL to the precompile returns the direct call's non-empty result."""
    forwarder = init_code(bytes.fromhex(FORWARDER_RUNTIME.format(addr=address[2:])))
    _, fwd = await deploy_contract(w3, chain_id=chain_id, private_key=funded_account.key.hex(), bytecode=forwarder)
    data = "0x" + calldata.hex()
    via_contract = bytes(await w3.eth.call({"to": fwd, "data": data}))
    direct = bytes(await w3.eth.call({"to": address, "data": data}))
    assert via_contract == direct and len(via_contract) > 0
