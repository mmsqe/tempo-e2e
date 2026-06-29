"""Native tempo (0x76) transaction semantics: type, batching, validity windows."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize, sign_transaction
from tempo.constants import PATH_USD

from .utils import build_tempo_tx, get_nonce, new_account, send_tempo_tx, suggested_max_fee

pytestmark = pytest.mark.tempo


async def _now(w3):
    return (await w3.eth.get_block("latest"))["timestamp"]


async def test_serialized_tx_uses_0x76_type(w3, chain_id, funded_account):
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 1).data}],
    )
    assert serialize(sign_transaction(tx, Signer(funded_account.key.hex()))).startswith("0x76")


async def test_batched_heterogeneous_calls_one_nonce(w3, chain_id, funded_account):
    recipient, spender = new_account().address, new_account().address
    nonce_before = await get_nonce(w3, funded_account.address)

    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=nonce_before,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[
            {"to": PATH_USD, "data": ERC20.fns.transfer(recipient, 321).data},
            {"to": PATH_USD, "data": ERC20.fns.approve(spender, 654).data},
        ],
    )
    receipt = await send_tempo_tx(w3, tx, funded_account.key.hex())

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 321
    assert await ERC20.fns.allowance(funded_account.address, spender).call(w3, to=PATH_USD) == 654
    assert await get_nonce(w3, funded_account.address) == nonce_before + 1


async def test_valid_before_in_past_is_rejected(w3, chain_id, funded_account):
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        valid_before=await _now(w3) - 100,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 1).data}],
    )
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))


async def test_valid_after_in_future_is_rejected(w3, chain_id, funded_account):
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=0,
        valid_after=await _now(w3) + 3600,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, 1).data}],
    )
    signed = sign_transaction(tx, Signer(funded_account.key.hex()))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(signed))
