"""TIP-20 factory lifecycle: create a token, grant ISSUER_ROLE, migrate an ERC-20's balances, and burn."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD, TIP20_FACTORY_ADDRESS

from .abi import TIP20, TIP20_FACTORY, TIP20_ROLES
from .utils import ISSUER_ROLE, STATE_WRITE_GAS, new_account, send_calls, token_from_receipt

pytestmark = pytest.mark.tempo

SCALE = 10**12  # 18-decimal ERC-20 units per 6-decimal TIP-20 unit


async def test_create_token_mint_and_burn(w3, chain_id, funded_account):
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
    token = token_from_receipt(created)
    assert await TIP20_FACTORY.fns.isTIP20(token).call(w3, to=TIP20_FACTORY_ADDRESS)
    assert await ERC20.fns.decimals().call(w3, to=token) == 6

    # An ERC-20's holders, minted at 6 decimals and rounded down: dust alone mints nothing.
    holder, dusty = new_account().address, new_account().address
    snapshot = {admin.address: 5 * 10**18, holder: 12_345_678_901_234_567_890, dusty: SCALE - 1}
    minted = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": token, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, admin.address).data},
            *({"to": token, "data": ERC20.fns.mint(h, a // SCALE).data} for h, a in snapshot.items() if a >= SCALE),
        ],
    )
    assert minted["status"] == 1
    assert [await ERC20.fns.balanceOf(h).call(w3, to=token) for h in snapshot] == [5_000_000, 12_345_678, 0]
    assert await ERC20.fns.totalSupply().call(w3, to=token) == 17_345_678

    burned = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": token, "data": TIP20.fns.burn(2_000_000).data}],
    )
    assert burned["status"] == 1
    assert await ERC20.fns.balanceOf(admin.address).call(w3, to=token) == 3_000_000
    assert await ERC20.fns.totalSupply().call(w3, to=token) == 15_345_678
