"""TIP-20 factory: create a token, grant ISSUER_ROLE, and mint."""

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo.constants import PATH_USD, TIP20_FACTORY_ADDRESS
from web3 import Web3

from .abi import TIP20_FACTORY, TIP20_ROLES
from .utils import STATE_WRITE_GAS, send_calls

pytestmark = pytest.mark.tempo

ISSUER_ROLE = keccak(text="ISSUER_ROLE")


def _created_token(receipt) -> str:
    """The token address from the factory's TokenCreated event (indexed topic 1)."""
    log = next(log for log in receipt["logs"] if log["address"].lower() == TIP20_FACTORY_ADDRESS.lower())
    return Web3.to_checksum_address(HexBytes(log["topics"][1])[-20:])


async def test_create_token_grant_role_and_mint(w3, chain_id, funded_account):
    admin = funded_account
    salt = b"\x07" + b"\x00" * 31

    created = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {
                "to": TIP20_FACTORY_ADDRESS,
                "data": TIP20_FACTORY.fns.createToken("MyUSD", "MUSD", "USD", PATH_USD, admin.address, salt).data,
            }
        ],
    )
    assert created["status"] == 1
    token = _created_token(created)
    assert await TIP20_FACTORY.fns.isTIP20(token).call(w3, to=TIP20_FACTORY_ADDRESS)

    minted = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, admin.address).data},
            {"to": token, "data": ERC20.fns.mint(admin.address, 5_000_000).data},
        ],
    )
    assert minted["status"] == 1
    assert await ERC20.fns.balanceOf(admin.address).call(w3, to=token) == 5_000_000
    assert await ERC20.fns.totalSupply().call(w3, to=token) == 5_000_000
