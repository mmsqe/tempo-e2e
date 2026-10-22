"""TIP-1060 storage credits (T7): deleting a slot mints a credit to its owner, a contract, never an EOA."""

import pytest
from tempo.constants import ALPHA_USD
from tempo.constants import STABLECOIN_DEX_ADDRESS as DEX_ADDR
from tempo.constants import STORAGE_CREDITS_ADDRESS as SC_ADDR

from .abi import DEX
from .abi import STORAGE_CREDITS as SC
from .utils import (
    STATE_WRITE_GAS,
    active_forks,
    approve_call,
    call_revert,
    create_token,
    deploy_contract,
    fund_token,
    funded,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo


# Constructor sets slot0=1; the runtime clears it on any call, minting the contract a credit.
CREATE_THEN_DELETABLE_INIT = bytes.fromhex("60016000556006601160003960066000f3600060005500")


async def test_fresh_account_reads_zero(w3):
    acct = new_account().address
    assert await SC.fns.balanceOf(acct).call(w3) == 0
    assert await SC.fns.modeOf(acct).call(w3) == 0  # Refund
    assert await SC.fns.budgetOf(acct).call(w3) == 0


async def test_reserved_mode_reverts(w3):
    reason = await call_revert(w3, SC_ADDR, SC.fns.setMode(3).data)
    if "T11" in await active_forks(w3):
        # Strict decoding refuses the out-of-range enum before dispatch, so no error selector.
        assert reason == "execution reverted"
    else:
        assert "InvalidMode" in reason or "0xa0042b17" in reason


async def test_valid_mode_and_budget_calls_succeed(w3, chain_id):
    payer = await funded(w3)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=payer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": SC_ADDR, "data": SC.fns.setMode(2).data},  # Direct
            {"to": SC_ADDR, "data": SC.fns.setBudget(7).data},
            {"to": SC_ADDR, "data": SC.fns.setMode(0).data},  # back to Refund
        ],
    )
    assert receipt["status"] == 1


async def test_slot_deletion_mints_credit_to_contract(w3, chain_id):
    deployer = await funded(w3)
    receipt, contract = await deploy_contract(
        w3, chain_id=chain_id, private_key=deployer.key.hex(), bytecode=CREATE_THEN_DELETABLE_INIT
    )
    assert receipt["status"] == 1
    # the slot creation nets to zero within the deploy tx (default Refund mode)
    assert await SC.fns.balanceOf(contract).call(w3) == 0

    # a later tx deletes the slot with no matching creation -> mints exactly one credit
    await send_call(w3, chain_id, deployer, contract, b"")
    assert await SC.fns.balanceOf(contract).call(w3) == 1


# Constructor sets slotE=1; empty calldata clears it (earning a credit), RECREATE calldata sets slotR.
RECREATE_GAS_INIT = bytes.fromhex("60016000556013601160003960136000f3600035600c576000600055005b600160015500")
RECREATE = (1).to_bytes(32, "big")


async def test_credit_reduces_recreate_gas(w3, chain_id):
    payer = await funded(w3)
    _, credited = await deploy_contract(w3, chain_id=chain_id, private_key=payer.key.hex(), bytecode=RECREATE_GAS_INIT)
    _, cold = await deploy_contract(w3, chain_id=chain_id, private_key=payer.key.hex(), bytecode=RECREATE_GAS_INIT)

    # earn a credit on `credited` (delete slotE in its own tx so nothing nets it away)
    await send_call(w3, chain_id, payer, credited, b"")
    assert await SC.fns.balanceOf(credited).call(w3) == 1

    gas_credited = (await send_call(w3, chain_id, payer, credited, RECREATE))["gasUsed"]
    gas_cold = (await send_call(w3, chain_id, payer, cold, RECREATE))["gasUsed"]
    # the credit refunds ~STORAGE_CREDIT_VALUE (245k) on the otherwise-identical slot creation
    assert 240_000 < gas_cold - gas_credited < 250_000


async def test_dex_order_cancel_credits_the_maker(w3, chain_id):
    maker = await funded(w3)
    await fund_token(w3, chain_id=chain_id, to=maker.address, token=ALPHA_USD, amount=10_000_000_000)
    assert await DEX.fns.storageCredits(maker.address).call(w3) == 0

    order_id = await DEX.fns.nextOrderId().call(w3)
    placed = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            approve_call(DEX_ADDR, ALPHA_USD),
            {"to": DEX_ADDR, "data": DEX.fns.place(ALPHA_USD, 2_000_000_000, False, 0).data},
        ],
    )
    assert placed["status"] == 1
    assert await DEX.fns.storageCredits(maker.address).call(w3) == 0  # placing does not credit

    await send_call(w3, chain_id, maker, DEX_ADDR, DEX.fns.cancel(order_id).data)
    # cancelling frees the order's storage slots, crediting the maker for a future reuse (TIP-1064)
    assert 0 < await DEX.fns.storageCredits(maker.address).call(w3) <= 6


async def test_dex_replace_consumes_maker_credits(w3, chain_id, funded_account):
    """TIP-1064: credits from a cancel pay for the maker's next identical placement."""
    maker = await funded(w3)
    token = await create_token(w3, chain_id=chain_id, admin=funded_account, mint=(maker.address, 10_000_000_000))
    await send_call(w3, chain_id, maker, **approve_call(DEX_ADDR, token))

    place = DEX.fns.place(token, 2_000_000_000, False, 0).data
    order_id = await DEX.fns.nextOrderId().call(w3)
    first = await send_call(w3, chain_id, maker, DEX_ADDR, place)  # no credits: full storage cost
    await send_call(w3, chain_id, maker, DEX_ADDR, DEX.fns.cancel(order_id).data)
    earned = await DEX.fns.storageCredits(maker.address).call(w3)
    assert earned > 0

    second = await send_call(w3, chain_id, maker, DEX_ADDR, place)  # reuses the freed slots
    assert await DEX.fns.storageCredits(maker.address).call(w3) < earned  # credits were consumed...
    assert second["gasUsed"] < first["gasUsed"]  # ...making the identical placement cheaper


async def test_mode_and_budget_are_transient(w3, chain_id):
    """TIP-1060: setMode/setBudget live in transient storage -- they reset between txs."""
    payer = await funded(w3)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=payer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": SC_ADDR, "data": SC.fns.setMode(2).data}, {"to": SC_ADDR, "data": SC.fns.setBudget(7).data}],
    )
    assert receipt["status"] == 1
    # the next tx (and eth_call) observes the defaults again: Refund mode, zero budget
    assert await SC.fns.modeOf(payer.address).call(w3) == 0
    assert await SC.fns.budgetOf(payer.address).call(w3) == 0
