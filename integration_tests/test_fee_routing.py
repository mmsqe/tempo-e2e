"""TIP-1033 two-hop FeeAMM routing: with no direct userToken->PATH pool, gas routes through
userToken.quoteToken(), so a token quoted in ALPHA pays X -> ALPHA -> PATH, moving both pools."""

import pytest
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, BETA_USD, PATH_USD

from .abi import FEE
from .utils import create_token, new_account, prepare_tx, seed_fee_pool, send_calls, transfer_call

pytestmark = pytest.mark.tempo

# Both hops of a fee swap apply the same M = 9970/10000 (30 bps) rate, floored per hop.
M, SCALE = 9970, 10000


def _one_hop(amount: int) -> int:
    return amount * M // SCALE


async def _token_for(w3, chain_id, admin, quote):
    """A fresh user holding only a fresh token quoted in `quote`; returns (user, token)."""
    user = new_account()
    token = await create_token(w3, chain_id=chain_id, admin=admin, quote=quote, mint=(user.address, 1_000_000_000))
    return user, token


async def _pay_gas(w3, chain_id, user, token):
    """Send a tx that pays its gas in `token`; assert it is mined."""
    calls = [transfer_call(new_account().address, 1, token)]
    receipt = await send_calls(w3, chain_id=chain_id, private_key=user.key.hex(), fee_token=token, calls=calls)
    assert receipt["status"] == 1


async def _gas_payment_rejected(w3, chain_id, user, token):
    """A tx paying gas in `token` has no viable fee route and is rejected at admission."""
    tx = await prepare_tx(w3, chain_id, user, [transfer_call(new_account().address, 1, token)], fee_token=token)
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(sign_transaction(tx, Signer(user.key.hex()))))


async def test_two_hop_route_moves_both_pools(w3, chain_id, funded_account):
    # X is quoted in ALPHA; seed only leg1 (X<->ALPHA) -- leg2 (ALPHA<->PATH) is genesis
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)

    legs = [(token, ALPHA_USD), (ALPHA_USD, PATH_USD)]
    before1, before2 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]
    await _pay_gas(w3, chain_id, user, token)
    after1, after2 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]

    x_in = after1[0] - before1[0]  # X paid into leg1 (the fee, in X)
    alpha_out = before1[1] - after1[1]  # ALPHA leaving leg1
    alpha_in = after2[0] - before2[0]  # ALPHA entering leg2
    path_out = before2[1] - after2[1]  # PATH leaving leg2 to the validator

    assert x_in > 0
    assert alpha_out == _one_hop(x_in)  # leg1: X -> ALPHA
    assert alpha_in == alpha_out  # the intermediate ALPHA is conserved across the two hops
    assert path_out == _one_hop(alpha_in)  # leg2: ALPHA -> PATH


async def test_direct_pool_preferred_over_two_hop(w3, chain_id, funded_account):
    """A liquid direct X<->PATH pool wins: routing never touches the X<->ALPHA leg."""
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=PATH_USD)  # direct
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)  # two-hop leg1

    legs = [(token, PATH_USD), (token, ALPHA_USD)]
    before_direct, before_leg1 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]
    await _pay_gas(w3, chain_id, user, token)
    after_direct, after_leg1 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]

    assert after_direct[0] > before_direct[0] and after_direct[1] < before_direct[1]  # direct pool absorbed it
    assert after_leg1 == before_leg1  # the two-hop leg1 was never consulted


async def test_no_route_reverts_when_legs_dry(w3, chain_id, funded_account):
    """No direct X<->PATH pool and a dry X<->ALPHA leg1 => no viable route; rejected at admission."""
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)  # neither pool seeded (leg2 is genesis)
    await _gas_payment_rejected(w3, chain_id, user, token)


async def test_quote_token_equals_validator_token_skips_two_hop(w3, chain_id, funded_account):
    """With quoteToken() == validatorToken (PATH) the two-hop fallback is skipped, so with no direct pool
    the tx is rejected even though a liquid X<->ALPHA<->PATH path exists."""
    user, token = await _token_for(w3, chain_id, funded_account, PATH_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)  # decoy leg
    await _gas_payment_rejected(w3, chain_id, user, token)


async def test_two_hop_haircut_compounds_m_squared(w3, chain_id, funded_account):
    """A two-hop swap applies M twice (M^2), floored per hop -- strictly less than one hop."""
    user, token = await _token_for(w3, chain_id, funded_account, ALPHA_USD)
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=ALPHA_USD)

    legs = [(token, ALPHA_USD), (ALPHA_USD, PATH_USD)]
    before1, before2 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]
    await _pay_gas(w3, chain_id, user, token)
    after1, after2 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]

    x_in = after1[0] - before1[0]
    path_out = before2[1] - after2[1]
    assert x_in > 0
    assert path_out == _one_hop(_one_hop(x_in))  # M^2, exactly
    assert path_out < _one_hop(x_in)  # strictly below one hop


@pytest.mark.parametrize("intermediate", [ALPHA_USD, BETA_USD])
async def test_two_hop_routes_through_quote_token(w3, chain_id, funded_account, intermediate):
    """The intermediate is userToken.quoteToken(), not a hardcoded token; both legs move, conserving it."""
    user, token = await _token_for(w3, chain_id, funded_account, intermediate)
    # seed leg2 first: leg1's seed tx pays its gas in `intermediate`, swappable only once leg2 exists
    await seed_fee_pool(w3, chain_id=chain_id, user_token=intermediate, validator_token=PATH_USD)  # leg2
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, validator_token=intermediate)  # leg1

    legs = [(token, intermediate), (intermediate, PATH_USD)]
    before1, before2 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]
    await _pay_gas(w3, chain_id, user, token)
    after1, after2 = [await FEE.fns.getPool(*leg).call(w3) for leg in legs]

    x_in = after1[0] - before1[0]
    mid_out = before1[1] - after1[1]
    mid_in = after2[0] - before2[0]
    path_out = before2[1] - after2[1]
    assert x_in > 0
    assert mid_out == _one_hop(x_in)
    assert mid_in == mid_out  # the quote token flows straight through, conserved
    assert path_out == _one_hop(mid_in)
