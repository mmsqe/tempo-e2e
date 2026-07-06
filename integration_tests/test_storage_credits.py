"""TIP-1060 storage credits (0x1060…, T7): deleting a storage slot mints a credit
to the slot's owner (a contract, never an EOA); mode 3 is reserved.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import ALPHA_USD
from tempo.constants import STABLECOIN_DEX_ADDRESS as DEX_ADDR
from tempo.constants import STORAGE_CREDITS_ADDRESS as SC_ADDR

from .abi import DEX
from .abi import STORAGE_CREDITS as SC
from .utils import (
    MAX_UINT,
    STATE_WRITE_GAS,
    call_revert,
    create_token,
    deploy_contract,
    fund,
    fund_token,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo


# Constructor SSTOREs slot0=1 (a creation), then deploys runtime `600060005500`
# which SSTOREs slot0=0 (a deletion) on any call -> mints a credit to the contract.
CREATE_THEN_DELETABLE_INIT = bytes.fromhex("60016000556006601160003960066000f3600060005500")


async def test_fresh_account_reads_zero(w3):
    acct = new_account().address
    assert await SC.fns.balanceOf(acct).call(w3, to=SC_ADDR) == 0
    assert await SC.fns.modeOf(acct).call(w3, to=SC_ADDR) == 0  # Refund
    assert await SC.fns.budgetOf(acct).call(w3, to=SC_ADDR) == 0


async def test_reserved_mode_reverts(w3):
    reason = await call_revert(w3, SC_ADDR, SC.fns.setMode(3).data)
    assert "InvalidMode" in reason or "0xa0042b17" in reason


async def test_valid_mode_and_budget_calls_succeed(w3, chain_id):
    payer = new_account()
    await fund(w3, payer.address)
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
    deployer = new_account()
    await fund(w3, deployer.address)
    receipt, contract = await deploy_contract(
        w3, chain_id=chain_id, private_key=deployer.key.hex(), bytecode=CREATE_THEN_DELETABLE_INIT
    )
    assert receipt["status"] == 1
    # the slot creation nets to zero within the deploy tx (default Refund mode)
    assert await SC.fns.balanceOf(contract).call(w3, to=SC_ADDR) == 0

    # a later tx deletes the slot with no matching creation -> mints exactly one credit
    await send_call(w3, chain_id, deployer, contract, b"")
    assert await SC.fns.balanceOf(contract).call(w3, to=SC_ADDR) == 1


# Constructor SSTOREs slotE=1; runtime EARN (empty calldata) deletes slotE (mints +1),
# RECREATE (nonzero calldata) creates slotR. In default Refund mode a held credit
# refunds the ~245k creation cost at settlement, lowering the recreate's gasUsed.
RECREATE_GAS_INIT = bytes.fromhex("60016000556013601160003960136000f3600035600c576000600055005b600160015500")
RECREATE = (1).to_bytes(32, "big")


async def test_credit_reduces_recreate_gas(w3, chain_id):
    payer = new_account()
    await fund(w3, payer.address)
    _, credited = await deploy_contract(w3, chain_id=chain_id, private_key=payer.key.hex(), bytecode=RECREATE_GAS_INIT)
    _, cold = await deploy_contract(w3, chain_id=chain_id, private_key=payer.key.hex(), bytecode=RECREATE_GAS_INIT)

    # earn a credit on `credited` (delete slotE in its own tx so nothing nets it away)
    await send_call(w3, chain_id, payer, credited, b"")
    assert await SC.fns.balanceOf(credited).call(w3, to=SC_ADDR) == 1

    gas_credited = (await send_call(w3, chain_id, payer, credited, RECREATE))["gasUsed"]
    gas_cold = (await send_call(w3, chain_id, payer, cold, RECREATE))["gasUsed"]
    # the credit refunds ~STORAGE_CREDIT_VALUE (245k) on the otherwise-identical slot creation
    assert 240_000 < gas_cold - gas_credited < 250_000


async def test_dex_order_cancel_credits_the_maker(w3, chain_id):
    maker = new_account()
    await fund(w3, maker.address)
    await fund_token(w3, chain_id=chain_id, to=maker.address, token=ALPHA_USD, amount=10_000_000_000)
    assert await DEX.fns.storageCredits(maker.address).call(w3, to=DEX_ADDR) == 0

    order_id = await DEX.fns.nextOrderId().call(w3, to=DEX_ADDR)
    placed = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=maker.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": ALPHA_USD, "data": ERC20.fns.approve(DEX_ADDR, MAX_UINT).data},
            {"to": DEX_ADDR, "data": DEX.fns.place(ALPHA_USD, 2_000_000_000, False, 0).data},
        ],
    )
    assert placed["status"] == 1
    assert await DEX.fns.storageCredits(maker.address).call(w3, to=DEX_ADDR) == 0  # placing does not credit

    await send_call(w3, chain_id, maker, DEX_ADDR, DEX.fns.cancel(order_id).data)
    # cancelling frees the order's storage slots, crediting the maker for a future reuse (TIP-1064)
    assert 0 < await DEX.fns.storageCredits(maker.address).call(w3, to=DEX_ADDR) <= 6


async def test_dex_replace_consumes_maker_credits(w3, chain_id, funded_account):
    """TIP-1064 consume side: credits earned by a cancel offset the storage cost of the
    maker's next placement -- the balance decrements and the identical place is cheaper."""
    maker = new_account()
    await fund(w3, maker.address)
    token = await create_token(w3, chain_id=chain_id, admin=funded_account, mint=(maker.address, 10_000_000_000))
    await send_call(w3, chain_id, maker, token, ERC20.fns.approve(DEX_ADDR, MAX_UINT).data)

    def _credits():
        return DEX.fns.storageCredits(maker.address).call(w3, to=DEX_ADDR)

    place = DEX.fns.place(token, 2_000_000_000, False, 0).data
    order_id = await DEX.fns.nextOrderId().call(w3, to=DEX_ADDR)
    first = await send_call(w3, chain_id, maker, DEX_ADDR, place)  # no credits: full storage cost
    await send_call(w3, chain_id, maker, DEX_ADDR, DEX.fns.cancel(order_id).data)
    earned = await _credits()
    assert earned > 0

    second = await send_call(w3, chain_id, maker, DEX_ADDR, place)  # reuses the freed slots
    assert await _credits() < earned  # credits were consumed...
    assert second["gasUsed"] < first["gasUsed"]  # ...making the identical placement cheaper


async def test_mode_and_budget_are_transient(w3, chain_id):
    """TIP-1060: setMode/setBudget live in transient storage -- they reset between txs."""
    payer = new_account()
    await fund(w3, payer.address)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=payer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": SC_ADDR, "data": SC.fns.setMode(2).data}, {"to": SC_ADDR, "data": SC.fns.setBudget(7).data}],
    )
    assert receipt["status"] == 1
    # the next tx (and eth_call) observes the defaults again: Refund mode, zero budget
    assert await SC.fns.modeOf(payer.address).call(w3, to=SC_ADDR) == 0
    assert await SC.fns.budgetOf(payer.address).call(w3, to=SC_ADDR) == 0
