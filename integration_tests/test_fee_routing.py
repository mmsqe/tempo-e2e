"""TIP-1033 two-hop FeeAMM routing: with no direct userToken->PATH pool, gas is
routed userToken -> userToken.quoteToken() -> PATH. A token quoted in ALPHA (itself
genesis-pooled to PATH) forces X -> ALPHA -> PATH, moving both pools in one payment.
"""

import pytest
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, BETA_USD, FEE_MANAGER_ADDRESS, PATH_USD

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

# Both hops of a fee swap apply the same M = 9970/10000 (30 bps) rate, floored per hop.
M, SCALE = 9970, 10000


def _one_hop(amount: int) -> int:
    return amount * M // SCALE


async def _pool(w3, user_token, validator_token):
    return await FEE.fns.getPool(user_token, validator_token).call(w3, to=FEE_MANAGER_ADDRESS)


async def _token_for(w3, chain_id, admin, quote):
    """A funded user holding a fresh token quoted in `quote`; returns (user, token)."""
    user = new_account()
    token = await create_token(w3, chain_id=chain_id, admin=admin, quote=quote, mint=(user.address, 1_000_000_000))
    return user, token


async def _gas_tx(w3, chain_id, token):
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        fee_token=token,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[transfer_call(new_account().address, 1, token)],
    )


async def _pay_gas(w3, chain_id, user, token):
    """Send a tx that pays its gas in `token`; assert it is mined."""
    receipt = await send_tempo_tx(w3, await _gas_tx(w3, chain_id, token), user.key.hex())
    assert receipt["status"] == 1


async def _gas_payment_rejected(w3, chain_id, user, token):
    """A tx paying gas in `token` has no viable fee route and is rejected at admission."""
    signed = sign_transaction(await _gas_tx(w3, chain_id, token), Signer(user.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))


async def test_two_hop_route_moves_both_pools(w3, chain_id, funded_account):
    # X is quoted in ALPHA; seed only leg1 (X<->ALPHA) -- leg2 (ALPHA<->PATH) is genesis
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)

    before1, before2 = await _pool(w3, token, ALPHA_USD), await _pool(w3, ALPHA_USD, PATH_USD)
    await _pay_gas(w3, chain_id, user, token)
    after1, after2 = await _pool(w3, token, ALPHA_USD), await _pool(w3, ALPHA_USD, PATH_USD)

    x_in = after1[0] - before1[0]  # X paid into leg1 (the fee, in X)
    alpha_out = before1[1] - after1[1]  # ALPHA leaving leg1
    alpha_in = after2[0] - before2[0]  # ALPHA entering leg2
    path_out = before2[1] - after2[1]  # PATH leaving leg2 to the validator

    # each hop is a fixed-rate swap with a 30 bps fee: amount_out = amount_in * 9970 // 10000
    assert x_in > 0
    assert alpha_out == _one_hop(x_in)  # leg1: X -> ALPHA
    assert alpha_in == alpha_out  # the intermediate ALPHA is conserved across the two hops
    assert path_out == _one_hop(alpha_in)  # leg2: ALPHA -> PATH


async def test_direct_pool_preferred_over_two_hop(w3, chain_id, funded_account):
    """With a liquid direct X<->PATH pool, routing takes the single hop and never touches the
    X<->ALPHA leg, even though X is quoted in ALPHA and a two-hop path exists."""
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=PATH_USD)  # direct
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)  # two-hop leg1

    before_direct, before_leg1 = await _pool(w3, token, PATH_USD), await _pool(w3, token, ALPHA_USD)
    await _pay_gas(w3, chain_id, user, token)
    after_direct, after_leg1 = await _pool(w3, token, PATH_USD), await _pool(w3, token, ALPHA_USD)

    assert after_direct[0] > before_direct[0] and after_direct[1] < before_direct[1]  # direct pool absorbed it
    assert after_leg1 == before_leg1  # the two-hop leg1 was never consulted


async def test_no_route_reverts_when_legs_dry(w3, chain_id, funded_account):
    """No direct X<->PATH pool and a dry X<->ALPHA leg1 => no viable route; rejected at admission."""
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)  # neither pool seeded (leg2 is genesis)
    await _gas_payment_rejected(w3, chain_id, user, token)


async def test_quote_token_equals_validator_token_skips_two_hop(w3, chain_id, funded_account):
    """When userToken.quoteToken() == validatorToken (PATH), the intermediate would equal the
    destination, so the two-hop fallback is skipped by the mid==validator guard. With no direct
    pool the tx is rejected -- a fully-liquid X<->ALPHA<->PATH path exists but is never considered,
    because the quote token (PATH), not ALPHA, drives routing."""
    user, token = await _token_for(w3, chain_id, funded_account, PATH_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)  # decoy leg
    await _gas_payment_rejected(w3, chain_id, user, token)


async def test_two_hop_haircut_compounds_m_squared(w3, chain_id, funded_account):
    """A two-hop swap applies M twice (M^2), floored per hop -- strictly less than one hop."""
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)

    before1, before2 = await _pool(w3, token, ALPHA_USD), await _pool(w3, ALPHA_USD, PATH_USD)
    await _pay_gas(w3, chain_id, user, token)
    after1, after2 = await _pool(w3, token, ALPHA_USD), await _pool(w3, ALPHA_USD, PATH_USD)

    x_in = after1[0] - before1[0]
    path_out = before2[1] - after2[1]
    assert x_in > 0
    assert path_out == _one_hop(_one_hop(x_in))  # M^2, exactly
    assert path_out < _one_hop(x_in)  # strictly below one hop


@pytest.mark.parametrize("intermediate", [ALPHA_USD, BETA_USD])
async def test_two_hop_routes_through_quote_token(w3, chain_id, funded_account, intermediate):
    """The intermediate is userToken.quoteToken(); parametrizing over ALPHA and BETA proves it is
    dynamic, not hardcoded. Both legs keyed on the quote token move, conserving it."""
    user, token = await _token_for(w3, chain_id, funded_account, intermediate)
    # seed leg2 first: leg1's seed tx pays its gas in `intermediate`, swappable only once leg2 exists
    await seed_fee_pool(w3, chain_id=chain_id, user_token=intermediate, validator_token=PATH_USD)  # leg2
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=intermediate)  # leg1

    before1, before2 = await _pool(w3, token, intermediate), await _pool(w3, intermediate, PATH_USD)
    await _pay_gas(w3, chain_id, user, token)
    after1, after2 = await _pool(w3, token, intermediate), await _pool(w3, intermediate, PATH_USD)

    x_in = after1[0] - before1[0]
    mid_out = before1[1] - after1[1]
    mid_in = after2[0] - before2[0]
    path_out = before2[1] - after2[1]
    assert x_in > 0
    assert mid_out == _one_hop(x_in)
    assert mid_in == mid_out  # the quote token flows straight through, conserved
    assert path_out == _one_hop(mid_in)
