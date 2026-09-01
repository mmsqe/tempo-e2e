"""EIP-7702 set-code: a type-0x04 authorization list delegates an EOA to ``0xef0100 || impl``.

The implementation only reads (returns a constant), sidestepping tempo's storage-credit
accounting for writes from a fresh account; the sponsor pays gas in the default stablecoin.
"""

import pytest
from hexbytes import HexBytes
from web3 import AsyncWeb3

from .utils import RETURN_42_INIT, deploy_contract, new_account, send_set_code_tx

pytestmark = pytest.mark.requires("tempo-native")

ZERO_ADDRESS = "0x" + "00" * 20


def _designator(delegate: str) -> HexBytes:
    return HexBytes("0xef0100" + AsyncWeb3.to_checksum_address(delegate)[2:])


async def test_account_starts_undelegated(w3, funded_account):
    assert await w3.eth.get_code(funded_account.address) == b""


async def test_set_code_delegation_installs_and_executes(w3, chain_id, funded_account):
    sponsor = funded_account
    _, delegate = await deploy_contract(w3, chain_id=chain_id, private_key=sponsor.key.hex(), bytecode=RETURN_42_INIT)
    authority = new_account()  # fresh EOA, nonce 0, no code
    assert await w3.eth.get_code(authority.address) == b""

    # `to == authority`: the same tx installs the delegation and calls into it, so the
    # implementation's RETURN(42) runs in the authority's context.
    receipt = await send_set_code_tx(
        w3,
        chain_id=chain_id,
        sponsor=sponsor,
        authority=authority,
        delegate=delegate,
        auth_nonce=0,
        to=authority.address,
    )
    assert receipt["status"] == 1
    assert receipt["type"] == 4
    assert HexBytes(await w3.eth.get_code(authority.address)) == _designator(delegate)
    assert await w3.eth.get_transaction_count(authority.address) == 1
    assert int.from_bytes(await w3.eth.call({"to": authority.address}), "big") == 42


async def test_revoke_delegation_clears_code(w3, chain_id, funded_account):
    sponsor = funded_account
    _, delegate = await deploy_contract(w3, chain_id=chain_id, private_key=sponsor.key.hex(), bytecode=RETURN_42_INIT)
    authority = new_account()
    await send_set_code_tx(
        w3,
        chain_id=chain_id,
        sponsor=sponsor,
        authority=authority,
        delegate=delegate,
        auth_nonce=0,
        to=authority.address,
    )
    assert HexBytes(await w3.eth.get_code(authority.address)) == _designator(delegate)

    # Delegating to the zero address clears the delegation (EIP-7702 revocation).
    receipt = await send_set_code_tx(
        w3,
        chain_id=chain_id,
        sponsor=sponsor,
        authority=authority,
        delegate=ZERO_ADDRESS,
        auth_nonce=1,
        to=sponsor.address,
    )
    assert receipt["status"] == 1
    assert await w3.eth.get_code(authority.address) == b""
    assert await w3.eth.get_transaction_count(authority.address) == 2
