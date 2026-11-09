"""Which stablecoin pays gas: the tempo tx's fee_token field, or the fallback list when nothing names one."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, BETA_USD, PATH_USD, THETA_USD
from web3.exceptions import Web3RPCError

from .utils import (
    FUNDED_AMOUNT,
    GENESIS_ADMIN,
    OUSD,
    PLAIN_CALL_GAS,
    fund_token,
    gas_cost_in_token,
    new_account,
    prepare_tx,
    rpc,
    seed_fee_pool,
    send_calls,
    send_type_2,
    sign_type_2,
    skip_before,
    suggested_max_fee,
    transfer_call,
)

pytestmark = pytest.mark.tempo

TARGET = GENESIS_ADMIN.address  # an existing account to call and pay


# A non-default fee token swaps through the FeeAMM, so it needs a pool; only ALPHA_USD has one at genesis.
@pytest.mark.parametrize("token", [ALPHA_USD, BETA_USD, THETA_USD])
async def test_gas_paid_in_chosen_stablecoin(w3, chain_id, token):
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token)
    acct = new_account()
    await fund_token(w3, chain_id=chain_id, to=acct.address, token=token, amount=5_000_000)
    before = await ERC20.fns.balanceOf(acct.address).call(w3, to=token)
    recipient = new_account().address

    calls = [transfer_call(recipient, 1000, token)]
    receipt = await send_calls(w3, chain_id=chain_id, private_key=acct.key.hex(), fee_token=token, calls=calls)

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=token) == 1000
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=token) == before - 1000 - gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=PATH_USD) == 0  # faucet token never involved


async def test_fee_token_without_balance_is_rejected(w3, chain_id, funded_account):
    """funded_account holds only PATH_USD; paying gas in ALPHA_USD must fail."""
    calls = [transfer_call(new_account().address, 1)]
    tx = await prepare_tx(w3, chain_id, funded_account, calls, fee_token=ALPHA_USD)
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))


@pytest.fixture
async def ousd(w3):
    if not await w3.eth.get_code(OUSD):
        pytest.skip("the chain has no OUSD, as NVNM's canary and testnet genesis do not")


@pytest.mark.usefixtures("ousd")
async def test_fallback_tries_pathusd_first(w3, chain_id, funded_account):
    """A payer holding pathUSD and OUSD that names no fee token pays in pathUSD."""
    payer = funded_account.address
    await fund_token(w3, chain_id=chain_id, to=payer, token=OUSD, amount=FUNDED_AMOUNT)
    before = await ERC20.fns.balanceOf(payer).call(w3, to=PATH_USD)

    receipt = await send_type_2(w3, funded_account, TARGET, b"", gas=PLAIN_CALL_GAS)

    assert await ERC20.fns.balanceOf(payer).call(w3, to=PATH_USD) == before - gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(payer).call(w3, to=OUSD) == FUNDED_AMOUNT


@pytest.mark.usefixtures("ousd")
async def test_fallback_pays_in_ousd_without_pathusd(w3, chain_id):
    """TIP-1115: from T13 a payer with no pathUSD pays in OUSD, the next fallback."""
    await skip_before(w3, "T13")
    payer = new_account()
    await fund_token(w3, chain_id=chain_id, to=payer.address, token=OUSD, amount=FUNDED_AMOUNT)

    receipt = await send_type_2(w3, payer, TARGET, b"", gas=PLAIN_CALL_GAS)

    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=OUSD) == FUNDED_AMOUNT - gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == 0


async def test_before_t13_only_pathusd_pays(held_back):
    """Before T13 the fallback is pathUSD alone, so a payer holding only OUSD is refused."""
    async with rpc(held_back("t13_time")) as w3:
        payer = new_account()
        chain_id = await w3.eth.chain_id
        # Paid in pathUSD: this node takes no fee in OUSD, which is the point.
        await fund_token(w3, chain_id=chain_id, to=payer.address, token=OUSD, amount=FUNDED_AMOUNT, fee_token=PATH_USD)
        with pytest.raises(Web3RPCError, match="insufficient funds .*have 0 want"):  # the pathUSD balance
            await send_type_2(w3, payer, TARGET, b"", gas=PLAIN_CALL_GAS)


@pytest.mark.usefixtures("ousd")
async def test_fallback_moves_on_when_pathusd_is_spent_first(w3, chain_id):
    """A second transaction admitted while pathUSD covered it pays in OUSD once the first spends that
    pathUSD, rather than being dropped for the token cached at admission (#8423)."""
    await skip_before(w3, "T13")
    payer = new_account()
    max_fee = await suggested_max_fee(w3)
    spend_gas, fallback_gas = 300_000, 1_000_000
    # Each one's most possible fee in 6-decimal units; the second's exceeds what the first leaves.
    spend_cap, fallback_cap = (-(-gas * max_fee // 10**12) for gas in (spend_gas, fallback_gas))
    await fund_token(w3, chain_id=chain_id, to=payer.address, token=PATH_USD, amount=spend_cap + fallback_cap)
    await fund_token(w3, chain_id=chain_id, to=payer.address, token=OUSD, amount=FUNDED_AMOUNT)

    # The transfer's fee token is inferred, pathUSD.
    spend = ERC20.fns.transfer(TARGET, fallback_cap).data
    first = await sign_type_2(w3, payer, PATH_USD, spend, gas=spend_gas, nonce=0, max_fee_per_gas=max_fee)
    second = await sign_type_2(w3, payer, TARGET, gas=fallback_gas, nonce=1, max_fee_per_gas=max_fee)
    hashes = [await w3.eth.send_raw_transaction(raw) for raw in (first, second)]
    spent, fell_back = [await w3.eth.wait_for_transaction_receipt(h, timeout=30) for h in hashes]

    assert (spent["status"], fell_back["status"]) == (1, 1)
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == spend_cap - gas_cost_in_token(spent)
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=OUSD) == FUNDED_AMOUNT - gas_cost_in_token(fell_back)
