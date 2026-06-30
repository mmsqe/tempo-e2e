"""TIP-20 enshrined stablecoin behavior: transfer, approve/transferFrom, batches."""

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo.constants import PATH_USD

from .abi import TIP20
from .utils import STABLECOINS, fund, gas_cost_in_token, get_nonce, new_account, send_calls, transfer_call

pytestmark = pytest.mark.tempo

# keccak256("TransferWithMemo(address,address,uint256,bytes32)")
TRANSFER_WITH_MEMO_TOPIC = HexBytes(keccak(text="TransferWithMemo(address,address,uint256,bytes32)"))


async def test_standard_tokens_have_supply(w3):
    for symbol, addr in STABLECOINS.items():
        assert await ERC20.fns.totalSupply().call(w3, to=addr) > 0, symbol


async def test_transfer_accounting_includes_gas(w3, chain_id, funded_account):
    recipient = new_account().address
    before = await ERC20.fns.balanceOf(funded_account.address).call(w3, to=PATH_USD)

    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), calls=[transfer_call(recipient, 1234)]
    )

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1234
    after = await ERC20.fns.balanceOf(funded_account.address).call(w3, to=PATH_USD)
    assert after == before - 1234 - gas_cost_in_token(receipt)


async def test_approve_and_allowance(w3, chain_id, funded_account):
    spender = new_account().address
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.approve(spender, 50_000).data}],
    )
    assert receipt["status"] == 1
    assert await ERC20.fns.allowance(funded_account.address, spender).call(w3, to=PATH_USD) == 50_000


async def test_transfer_from_spends_allowance(w3, chain_id):
    owner, spender = new_account(), new_account()
    await fund(w3, owner.address)
    await fund(w3, spender.address)  # spender needs balance to pay gas
    recipient = new_account().address

    await send_calls(
        w3,
        chain_id=chain_id,
        private_key=owner.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.approve(spender.address, 40_000).data}],
    )
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=spender.key.hex(),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transferFrom(owner.address, recipient, 30_000).data}],
    )

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 30_000
    assert await ERC20.fns.allowance(owner.address, spender.address).call(w3, to=PATH_USD) == 10_000


async def test_batched_transfers_in_one_tx(w3, chain_id, funded_account):
    r1, r2 = new_account().address, new_account().address
    nonce_before = await get_nonce(w3, funded_account.address)

    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[transfer_call(r1, 111), transfer_call(r2, 222)],
    )

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(r1).call(w3, to=PATH_USD) == 111
    assert await ERC20.fns.balanceOf(r2).call(w3, to=PATH_USD) == 222
    assert await get_nonce(w3, funded_account.address) == nonce_before + 1


async def test_transfer_with_memo_emits_memo(w3, chain_id, funded_account):
    recipient = new_account().address
    memo = b"\xab" + b"\x00" * 31
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[{"to": PATH_USD, "data": TIP20.fns.transferWithMemo(recipient, 777, memo).data}],
    )
    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 777
    memo_logs = [log for log in receipt["logs"] if log["topics"][0] == TRANSFER_WITH_MEMO_TOPIC]
    assert memo_logs and memo_logs[0]["topics"][3] == HexBytes(memo)


async def test_transfer_exceeding_balance_reverts(w3, chain_id, funded_account):
    balance = await ERC20.fns.balanceOf(funded_account.address).call(w3, to=PATH_USD)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funded_account.key.hex(),
        calls=[transfer_call(new_account().address, balance + 1)],
    )
    assert receipt["status"] == 0
