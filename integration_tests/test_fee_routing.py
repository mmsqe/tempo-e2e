"""TIP-1033 two-hop FeeAMM routing: with no direct userToken->PATH pool, gas is
routed userToken -> userToken.quoteToken() -> PATH. A token quoted in ALPHA (itself
genesis-pooled to PATH) forces X -> ALPHA -> PATH, moving both pools in one payment.
"""

import pytest
from tempo.constants import ALPHA_USD, FEE_MANAGER_ADDRESS, PATH_USD

from .abi import FEE
from .utils import (
    build_tempo_tx,
    create_token,
    new_account,
    seed_fee_pool,
    send_tempo_tx,
    suggested_max_fee,
    transfer_call,
)

pytestmark = pytest.mark.tempo


async def test_two_hop_route_moves_both_pools(w3, chain_id, funded_account):
    user = new_account()
    # X is quoted in ALPHA; seed only leg1 (X<->ALPHA) -- leg2 (ALPHA<->PATH) is genesis
    token = await create_token(
        w3, chain_id=chain_id, admin=funded_account, quote=ALPHA_USD, mint=(user.address, 1_000_000_000)
    )
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)

    before_leg1 = await FEE.fns.getPool(token, ALPHA_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    before_leg2 = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)

    # a modest gas limit keeps the routed max-amount within the seeded reserves
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        fee_token=token,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1, token)],
    )
    assert (await send_tempo_tx(w3, tx, user.key.hex()))["status"] == 1

    after_leg1 = await FEE.fns.getPool(token, ALPHA_USD).call(w3, to=FEE_MANAGER_ADDRESS)
    after_leg2 = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3, to=FEE_MANAGER_ADDRESS)

    assert after_leg1[0] > before_leg1[0]  # leg1: X paid in
    assert after_leg1[1] < before_leg1[1]  # leg1: ALPHA paid out
    assert after_leg2[0] > before_leg2[0]  # leg2: that ALPHA paid in
    assert after_leg2[1] < before_leg2[1]  # leg2: PATH paid out to the validator
