import pytest
from eth_utils import to_checksum_address
from tempo.constants import (
    ALPHA_USD,
    FEE_MANAGER_ADDRESS,
    NONCE_ADDRESS,
    PATH_USD,
    STABLECOIN_DEX_ADDRESS,
    TIP20_FACTORY_ADDRESS,
    TIP403_REGISTRY_ADDRESS,
    VALIDATOR_CONFIG_ADDRESS,
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
from .utils import deploy_contract

pytestmark = pytest.mark.tempo

# Standard EVM identity precompile: echoes its input unchanged.
IDENTITY_PRECOMPILE = "0x0000000000000000000000000000000000000004"

# Precompile addresses not in the pinned tempo-py release yet (added to
# tempo.constants upstream; use literals until the git dep updates).
VALIDATOR_CONFIG_V2_ADDRESS = to_checksum_address("0xCccCcCCC00000000000000000000000000000001")
ADDRESS_REGISTRY_ADDRESS = to_checksum_address("0xFDC0000000000000000000000000000000000000")
STORAGE_CREDITS_ADDRESS = to_checksum_address("0x1060000000000000000000000000000000000000")
TIP20_CHANNEL_RESERVE_ADDRESS = to_checksum_address("0x4D50500000000000000000000000000000000000")

# Reused / long calldatas (kept short for the case table below).
_VALIDATOR_COUNT = bytes(VALIDATOR_CONFIG.fns.validatorCount().data)
_IS_VIRTUAL = bytes(ADDRESS_REGISTRY.fns.isVirtualAddress(PATH_USD).data)
_DOMAIN_SEPARATOR = bytes(TIP20_CHANNEL_RESERVE.fns.domainSeparator().data)

# One deterministic (pure/view) getter per enshrined precompile, each returning a
# single 32-byte word: (label, address, calldata). The identity precompile is an
# EVM baseline. The dev genesis activates all hardforks, so T3+/T7 precompiles
# (AddressRegistry, StorageCredits) are live.
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


def _staticcall_forwarder(precompile: str) -> bytes:
    """EVM init code for a contract that STATICCALLs ``precompile`` with the
    call's data and returns the first 32 bytes of its output (every precompile
    probed here returns a single 32-byte word)."""
    addr = bytes.fromhex(precompile.removeprefix("0x"))
    assert len(addr) == 20, "precompile must be a 20-byte address"
    runtime = bytes(
        [
            0x36,
            0x60,
            0x00,
            0x60,
            0x00,
            0x37,  # calldatacopy(dest=0, off=0, size=calldatasize)
            0x60,
            0x20,
            0x60,
            0x00,
            0x36,
            0x60,
            0x00,  # STATICCALL args: retSize=32, retOff=0, argsSize=cds, argsOff=0
            0x73,
            *addr,  # push20 <precompile>
            0x5A,
            0xFA,
            0x50,  # gas ; STATICCALL ; POP(success)
            0x60,
            0x20,
            0x60,
            0x00,
            0xF3,  # return(off=0, size=32)
        ]
    )
    n = len(runtime)
    # init: codecopy(dest=0, off=12, size=n) ; return(0, n)  -- 12-byte prefix
    init = bytes([0x60, n, 0x60, 0x0C, 0x60, 0x00, 0x39, 0x60, n, 0x60, 0x00, 0xF3])
    return init + runtime


async def _deploy_forwarder(w3, chain_id, acct, precompile: str) -> str:
    _, address = await deploy_contract(
        w3, chain_id=chain_id, private_key=acct.key.hex(), bytecode=_staticcall_forwarder(precompile)
    )
    return address


@pytest.mark.parametrize("label,address,calldata", _PRECOMPILE_CASES, ids=[c[0] for c in _PRECOMPILE_CASES])
async def test_precompile_callable_from_contract(w3, chain_id, funded_account, label, address, calldata):
    """A deployed contract STATICCALLs an enshrined tempo precompile and returns
    its output -- the E5 'contract -> precompile, deterministic result' path. The
    contract-forwarded result must match a direct call and be non-empty."""
    fwd = await _deploy_forwarder(w3, chain_id, funded_account, address)
    data = "0x" + calldata.hex()
    via_contract = bytes(await w3.eth.call({"to": fwd, "data": data}))
    direct = bytes(await w3.eth.call({"to": address, "data": data}))
    assert via_contract == direct and len(via_contract) > 0
