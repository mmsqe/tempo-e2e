"""``tempo_simulateV1``: eth_simulateV1 plus TIP-20 metadata for the tokens a call touches.

Wallets estimate with this before an account is funded: the call reverts for want of
balance and still reports the gas it burned, which is what sizes the fee reserve.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import TRANSFER_TOPIC, fund, new_account

pytestmark = pytest.mark.tempo


async def simulate(w3, calls) -> dict:
    """``tempo_simulateV1`` over one block of ``calls``."""
    payload = {"blockStateCalls": [{"calls": calls}]}
    resp = await w3.provider.make_request("tempo_simulateV1", [payload, "latest"])
    assert resp.get("error") is None, resp["error"]
    return resp["result"]


def transfer(sender: str, recipient: str, amount: int = 1000) -> dict:
    return {"from": sender, "to": PATH_USD, "input": "0x" + bytes(ERC20.fns.transfer(recipient, amount).data).hex()}


async def simulate_transfer(w3, sender: str, recipient: str, amount: int = 1000) -> dict:
    return await simulate(w3, [transfer(sender, recipient, amount)])


def call_result(result: dict) -> dict:
    """The one call of a one-call simulation."""
    return result["blocks"][0]["calls"][0]


async def test_reverting_call_reports_gas_where_estimate_refuses(w3):
    """What lets a wallet size its fee reserve before it has been funded at all.

    ``eth_estimateGas`` has nothing to return for a call that reverts, so it errors;
    the simulation reports the same revert *and* the gas that reached it.
    """
    poor, recipient = new_account().address, new_account().address

    estimate = await w3.provider.make_request("eth_estimateGas", [transfer(poor, recipient)])
    assert "InsufficientBalance" in estimate["error"]["message"]

    call = call_result(await simulate_transfer(w3, poor, recipient))
    assert call["status"] == "0x0"
    assert "InsufficientBalance" in str(call.get("error", ""))
    assert int(call["gasUsed"], 16) > 0


async def test_successful_transfer_is_traced_with_token_metadata(w3):
    sender, recipient = new_account(), new_account().address
    await fund(w3, sender.address)

    result = await simulate_transfer(w3, sender.address, recipient)
    call = call_result(result)

    assert call["status"] == "0x1"
    log = next(lg for lg in call["logs"] if lg["address"].lower() == PATH_USD.lower())
    assert log["topics"][0] == TRANSFER_TOPIC.to_0x_hex()
    assert int(log["data"], 16) == 1000

    # the metadata is the token's own view of itself, resolved server-side
    meta = result["tokenMetadata"][PATH_USD.lower()]
    assert meta["name"] == await ERC20.fns.name().call(w3, to=PATH_USD)
    assert meta["symbol"] == await ERC20.fns.symbol().call(w3, to=PATH_USD)
    assert meta["currency"] == "USD"


async def test_no_metadata_for_calls_that_touch_no_tip20(w3):
    result = await simulate(w3, [{"from": new_account().address, "to": new_account().address}])
    assert result["tokenMetadata"] == {}


async def test_simulation_does_not_touch_chain_state(w3):
    sender, recipient = new_account(), new_account().address
    await fund(w3, sender.address)
    before = await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD)

    assert call_result(await simulate_transfer(w3, sender.address, recipient))["status"] == "0x1"

    assert await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD) == before
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0
    assert await w3.eth.get_transaction_count(sender.address) == 0


async def test_later_call_in_a_block_sees_the_earlier_one(w3):
    """Calls compose within a block, so a wallet can simulate a sequence it has not sent.

    The second transfer can only succeed on a balance the first one moved -- and that
    balance never exists on chain.
    """
    sender, middle, recipient = new_account(), new_account().address, new_account().address
    await fund(w3, sender.address)

    first, second = (await simulate(w3, [transfer(sender.address, middle, 5000), transfer(middle, recipient, 3000)]))[
        "blocks"
    ][0]["calls"]

    assert (first["status"], second["status"]) == ("0x1", "0x1")
    assert await ERC20.fns.balanceOf(middle).call(w3, to=PATH_USD) == 0
