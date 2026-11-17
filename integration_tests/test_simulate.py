"""``tempo_simulateV1``: eth_simulateV1 plus TIP-20 metadata for the tokens a call touches."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import TRANSFER_TOPIC, funded, new_account, rpc_result, suggested_max_fee

pytestmark = pytest.mark.tempo


async def simulate(w3, calls) -> dict:
    """``tempo_simulateV1`` over one block of ``calls``."""
    return await rpc_result(w3, "tempo_simulateV1", [{"blockStateCalls": [{"calls": calls}]}, "latest"])


def transfer(sender: str, recipient: str, amount: int = 1000) -> dict:
    return {"from": sender, "to": PATH_USD, "input": "0x" + bytes(ERC20.fns.transfer(recipient, amount).data).hex()}


def call_result(result: dict) -> dict:
    """The one call of a one-call simulation."""
    return result["blocks"][0]["calls"][0]


async def test_reverting_call_reports_gas_where_estimate_refuses(w3):
    """An unfunded wallet sizes its fee reserve from this; ``eth_estimateGas`` just errors."""
    poor, recipient = new_account().address, new_account().address

    estimate = await w3.provider.make_request("eth_estimateGas", [transfer(poor, recipient)])
    assert "InsufficientBalance" in estimate["error"]["message"]

    call = call_result(await simulate(w3, [transfer(poor, recipient)]))
    assert call["status"] == "0x0"
    assert "InsufficientBalance" in str(call.get("error", ""))
    assert int(call["gasUsed"], 16) > 0


async def test_successful_transfer_is_traced_with_token_metadata(w3):
    sender, recipient = await funded(w3), new_account().address

    result = await simulate(w3, [transfer(sender.address, recipient)])
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
    sender, recipient = await funded(w3), new_account().address
    before = await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD)

    assert call_result(await simulate(w3, [transfer(sender.address, recipient)]))["status"] == "0x1"

    assert await ERC20.fns.balanceOf(sender.address).call(w3, to=PATH_USD) == before
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 0
    assert await w3.eth.get_transaction_count(sender.address) == 0


async def test_later_call_in_a_block_sees_the_earlier_one(w3):
    """The second transfer spends a balance only the first one moved, which never exists on chain."""
    sender, middle, recipient = await funded(w3), new_account().address, new_account().address

    first, second = (await simulate(w3, [transfer(sender.address, middle, 5000), transfer(middle, recipient, 3000)]))[
        "blocks"
    ][0]["calls"]

    assert (first["status"], second["status"]) == ("0x1", "0x1")
    assert await ERC20.fns.balanceOf(middle).call(w3, to=PATH_USD) == 0


async def eth_simulate(w3, calls) -> dict:
    """The one call of ``eth_simulateV1`` over one block of ``calls``."""
    blocks = await rpc_result(w3, "eth_simulateV1", [{"blockStateCalls": [{"calls": calls}]}, "latest"])
    return blocks[0]["calls"][0]


def transferred_to(call: dict) -> list[str]:
    """The recipients of the TIP-20 transfers a simulated call logged."""
    return ["0x" + log["topics"][2][-40:] for log in call["logs"] if log["topics"][0] == TRANSFER_TOPIC.to_0x_hex()]


async def test_a_call_naming_a_gas_price_is_not_a_system_tx(w3):
    """``gasPrice`` makes a legacy tx, which the all-zero placeholder signature once marked as a system tx."""
    sender, recipient = await funded(w3), new_account().address
    call = await eth_simulate(
        w3, [transfer(sender.address, recipient) | {"gasPrice": hex(await suggested_max_fee(w3))}]
    )
    assert call["status"] == "0x1", call
    assert recipient.lower() in transferred_to(call)


async def test_a_batch_simulates_as_its_own_calls(w3):
    """No ``to`` once became a trailing CREATE, which a batch may not carry after its calls."""
    sender, first, second = await funded(w3), new_account().address, new_account().address
    calls = [{"to": PATH_USD, "value": "0x0", "input": transfer(sender.address, to)["input"]} for to in (first, second)]
    call = await eth_simulate(w3, [{"from": sender.address, "calls": calls}])
    assert call["status"] == "0x1", call
    assert transferred_to(call) == [first.lower(), second.lower()]


async def test_a_call_from_a_sender_without_fee_tokens_names_its_fee(w3):
    """``eth_call`` charges no fee, so a named fee token and price do not ask the sender to hold ``gas * maxFeePerGas``.
    ``eth_estimateGas`` still caps gas at what the balance buys, so it refuses."""
    poor = new_account().address
    request = {"from": poor, "to": PATH_USD, "input": "0x" + bytes(ERC20.fns.balanceOf(poor).data).hex()}
    request |= {"feeToken": PATH_USD, "gas": hex(1_000_000), "maxFeePerGas": hex(await suggested_max_fee(w3))}
    assert int(await rpc_result(w3, "eth_call", [request, "latest"]), 16) == 0
