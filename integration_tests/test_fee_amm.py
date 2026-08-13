"""Fee AMM: paying gas in a non-validator stablecoin swaps through the FeeManager pool."""

import pytest
from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, FEE_MANAGER_ADDRESS, PATH_USD, THETA_USD

from .abi import FEE
from .utils import (
    build_tempo_tx,
    create_token,
    fund_token,
    gas_cost_in_token,
    new_account,
    seed_fee_pool,
    send_calls,
    send_tempo_tx,
    suggested_max_fee,
    transfer_call,
)

pytestmark = pytest.mark.tempo


async def test_mint_seeds_pool_for_ungenesised_token(w3, chain_id):
    """A stablecoin the genesis didn't seed (THETA) becomes gas-payable after FeeManager.mint."""
    await seed_fee_pool(w3, chain_id=chain_id, user_token=THETA_USD)
    _, reserve_validator = await FEE.fns.getPool(THETA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    assert reserve_validator > 0  # validator (PATH) side now funded, enabling THETA->PATH swaps


async def test_pool_id_is_deterministic(w3):
    pool_id = await FEE.fns.getPoolId(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    assert pool_id == await FEE.fns.getPoolId(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    assert pool_id != b"\x00" * 32


async def test_pool_has_reserves(w3):
    _, reserve_validator = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    assert reserve_validator > 0  # PATH side is seeded in genesis


async def test_set_user_token(w3, chain_id, funded_account):
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": FEE_MANAGER_ADDRESS, "data": FEE.fns.setUserToken(ALPHA_USD).data}],
    )
    assert receipt["status"] == 1
    stored = await FEE.fns.userTokens(funded_account.address).call(w3, to=FEE_MANAGER_ADDRESS)
    assert HexBytes(stored) == HexBytes(ALPHA_USD)


async def test_fee_in_non_validator_token_moves_pool(w3, chain_id):
    """Gas paid in ALPHA (the validator wants PATH) is swapped via the AMM, shifting reserves."""
    before = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    user = new_account()
    await fund_token(w3, chain_id=chain_id, to=user.address, token=ALPHA_USD, amount=50_000_000_000)

    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        fee_token=ALPHA_USD,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1, ALPHA_USD)],
    )
    assert (await send_tempo_tx(w3, tx, user.key.hex()))["status"] == 1

    after = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    assert after[0] > before[0]  # ALPHA (user token) reserve grew
    assert after[1] < before[1]  # PATH (validator token) reserve shrank


async def test_creator_seeds_its_own_pool_and_pays_gas_in_it(w3, chain_id, funded_account):
    """Self-service e2e: one ordinary account creates a TIP-20, seeds its fee pool, then pays gas in it."""
    creator, minted = funded_account, 10_000_000
    token = await create_token(w3, chain_id=chain_id, admin=creator, mint=(creator.address, minted))
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, funder_pk=creator.key.hex())

    pool_id = await FEE.fns.getPoolId(token, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    assert await FEE.fns.liquidityBalances(pool_id, creator.address).call(w3, to=FEE_MANAGER_ADDRESS) > 0
    # mint() only pulls validator side, so seeding cost the creator none of its own token
    assert await ERC20.fns.balanceOf(creator.address).call(w3, to=token) == minted

    before = await FEE.fns.getPool(token, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=creator.key.hex(),
        fee_token=token,
        calls=[transfer_call(new_account().address, 1000, token)],
    )
    assert receipt["status"] == 1
    after = await FEE.fns.getPool(token, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)

    fee = gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(creator.address).call(w3, to=token) == minted - 1000 - fee
    assert after[0] - before[0] == fee  # fee landed in the pool the creator seeded
    assert after[1] < before[1]  # and PATH left it for validator


async def test_insufficient_liquidity_names_the_fee_token(w3, chain_id, funded_account):
    """Paying gas in a no-pool token is rejected with an error that names the fee token (#6698)."""
    payer = new_account()
    # A fresh USD TIP-20 with no fee pool, payer holds enough that the swap, not the balance, is what fails.
    token = await create_token(w3, chain_id=chain_id, admin=funded_account, mint=(payer.address, 10_000_000_000))

    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        fee_token=token,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1, token)],
    )
    raw = serialize(sign_transaction(tx, Signer(payer.key.hex())))
    resp = await w3.provider.make_request("eth_sendRawTransaction", [raw])

    msg = (resp.get("error") or {}).get("message", "")
    assert "insufficient liquidity in FeeAMM pool to swap fee tokens" in msg, resp
    assert "(required:" in msg, resp  # required amount
    assert token.lower() in msg.lower(), msg  # identifies the offending fee token
