"""TIP-20 factory lifecycle: create a token, grant ISSUER_ROLE, migrate an ERC-20's balances, and burn."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD
from web3.exceptions import ContractLogicError

from .abi import TIP20, TIP20_FACTORY, TIP20_ROLES
from .utils import (
    ISSUER_ROLE,
    STATE_WRITE_GAS,
    blacklist_token,
    call_revert,
    create_token,
    funded,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo

SCALE = 10**12  # 18-decimal ERC-20 units per 6-decimal TIP-20 unit


async def test_create_token_mint_and_burn(w3, chain_id, funded_account):
    admin = funded_account
    token = await create_token(w3, chain_id=chain_id, admin=admin, name="MyUSD")
    assert await TIP20_FACTORY.fns.isTIP20(token).call(w3)
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

    await send_call(w3, chain_id, admin, token, TIP20.fns.burn(2_000_000).data)
    assert await ERC20.fns.balanceOf(admin.address).call(w3, to=token) == 3_000_000
    assert await ERC20.fns.totalSupply().call(w3, to=token) == 15_345_678


async def test_burn_at(w3, chain_id):
    """TIP-1006: a ``BURN_AT_ROLE`` holder burns any holder's balance, blocked or not."""
    try:
        role = await TIP20.fns.BURN_AT_ROLE().call(w3, to=PATH_USD)
    except ContractLogicError:
        pytest.skip("the node has no TIP-1006")
    admin, burner, holder = await funded(w3), await funded(w3), new_account().address
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(holder, 1_000))
    await send_call(w3, chain_id, admin, token, TIP20_ROLES.fns.grantRole(role, burner.address).data)
    burn = TIP20.fns.burnAt(holder, 400).data

    # The admin, also the issuer, lacks the role.
    assert "Unauthorized" in await call_revert(w3, token, burn, sender=admin.address)
    await send_call(w3, chain_id, burner, token, burn)
    await blacklist_token(w3, chain_id=chain_id, admin=admin, token=token, blocked=holder)
    await send_call(w3, chain_id, burner, token, burn)
    assert await ERC20.fns.balanceOf(holder).call(w3, to=token) == 200
    assert await ERC20.fns.totalSupply().call(w3, to=token) == 200

    pooled = TIP20.fns.burnAt(FEE_MANAGER_ADDRESS, 0).data
    assert "ProtectedAddress" in await call_revert(w3, token, pooled, sender=burner.address)
