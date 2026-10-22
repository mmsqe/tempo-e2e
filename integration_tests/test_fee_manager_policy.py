"""TIP-1042 FeeManager policy (T8+): collecting a fee needs an authorized payer but not the FeeManager as
an authorized recipient, while mint and burn gain the authorization checks they used to skip."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize, sign_transaction
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD

from .abi import FEE
from .utils import (
    blacklist_token,
    call_revert,
    create_token,
    gas_cost_in_token,
    new_account,
    prepare_tx,
    seed_fee_pool,
    send_call,
    send_calls,
    transfer_call,
)

pytestmark = pytest.mark.tempo

POOL_SEED = 50_000_000_000
BALANCE = 10_000_000


async def _fee_token(w3, chain_id, admin, *, holder: str, blocked: str) -> str:
    """A gas-payable TIP-20 held by ``holder`` that blacklists ``blocked``; the pool is seeded first,
    since at T8 mint needs the FeeManager to be an authorized recipient."""
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(holder, BALANCE))
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token)
    await blacklist_token(w3, chain_id=chain_id, admin=admin, token=token, blocked=blocked)
    return token


async def test_gas_payable_in_token_that_blacklists_the_fee_manager(w3, chain_id, funded_account):
    """The payer is authorized, the FeeManager is not, and the fee transfer into it goes through anyway."""
    payer = new_account()
    token = await _fee_token(w3, chain_id, funded_account, holder=payer.address, blocked=FEE_MANAGER_ADDRESS)
    recipient = new_account().address

    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=payer.key.hex(),
        fee_token=token,
        calls=[transfer_call(recipient, 1000, token)],
    )

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=token) == 1000
    spent = BALANCE - await ERC20.fns.balanceOf(payer.address).call(w3, to=token)
    assert spent == 1000 + gas_cost_in_token(receipt)  # gas really was collected in the token


async def test_fee_collection_still_requires_an_authorized_sender(w3, chain_id, funded_account):
    """T8 dropped the recipient check, not the sender check: a blacklisted payer can't pay gas."""
    payer = new_account()
    token = await _fee_token(w3, chain_id, funded_account, holder=payer.address, blocked=payer.address)

    tx = await prepare_tx(w3, chain_id, payer, [transfer_call(new_account().address, 1000, token)], fee_token=token)
    signed = sign_transaction(tx, Signer(payer.key.hex()))
    with pytest.raises(Exception) as rejected:
        await w3.eth.send_raw_transaction(serialize(signed))
    assert "PolicyForbids" in str(rejected.value)  # rejected at admission, not for an unrelated reason


async def test_mint_requires_the_fee_manager_as_recipient(w3, chain_id, funded_account):
    """mint moves tokens into the FeeManager, so from T8 it must be an authorized recipient."""
    admin = funded_account
    token = await _fee_token(w3, chain_id, admin, holder=admin.address, blocked=FEE_MANAGER_ADDRESS)

    data = FEE.fns.mint(token, PATH_USD, POOL_SEED, admin.address).data
    assert "PolicyForbids" in await call_revert(w3, FEE_MANAGER_ADDRESS, data, sender=admin.address)


async def test_burn_requires_an_authorized_sender(w3, chain_id, funded_account):
    """T8 gates burn on the caller being an authorized sender of both pool tokens."""
    lp = funded_account
    token = await create_token(w3, chain_id=chain_id, admin=lp, mint=(lp.address, BALANCE))
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, funder_pk=lp.key.hex())
    pool_id = await FEE.fns.getPoolId(token, PATH_USD).call(w3)
    liquidity = await FEE.fns.liquidityBalances(pool_id, lp.address).call(w3)
    half = liquidity // 2
    assert half > 0

    # Unrestricted, the LP burns its own liquidity back out.
    await send_call(w3, chain_id, lp, FEE_MANAGER_ADDRESS, FEE.fns.burn(token, PATH_USD, half, lp.address).data)
    assert await FEE.fns.liquidityBalances(pool_id, lp.address).call(w3) == liquidity - half

    # Blacklisted on the user token, the same burn is unauthorized.
    await blacklist_token(w3, chain_id=chain_id, admin=lp, token=token, blocked=lp.address)
    data = FEE.fns.burn(token, PATH_USD, half, lp.address).data
    assert "PolicyForbids" in await call_revert(w3, FEE_MANAGER_ADDRESS, data, sender=lp.address)
