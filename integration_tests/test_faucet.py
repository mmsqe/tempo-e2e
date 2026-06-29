"""The tempo_fundAddress faucet RPC."""

from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .network import FAUCET_AMOUNT
from .utils import fund, new_account, send_calls


async def test_fund_returns_tx_hashes(w3):
    result = await fund(w3, new_account().address)
    assert isinstance(result, list) and len(result) >= 1


async def test_fund_credits_expected_amount(w3):
    acct = new_account()
    await fund(w3, acct.address)
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=PATH_USD) == FAUCET_AMOUNT


async def test_repeated_funding_accumulates(w3):
    acct = new_account()
    await fund(w3, acct.address)
    await fund(w3, acct.address)
    assert await ERC20.fns.balanceOf(acct.address).call(w3, to=PATH_USD) == 2 * FAUCET_AMOUNT


async def test_funded_account_can_spend(w3, chain_id):
    acct = new_account()
    await fund(w3, acct.address)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=acct.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 1_000).data}],
    )
    assert receipt["status"] == 1
