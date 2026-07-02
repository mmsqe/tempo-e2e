"""TIP-1067 dynamic base fee: a block whose gas exceeds the target raises the next
block's baseFeePerGas; idle blocks decay it back toward the floor. A burst of
state-creating transfers congests a block, and the surrounding idle blocks decay.
"""

import asyncio

import pytest

from .utils import STATE_WRITE_GAS, fund, new_account, send_calls, transfer_call

pytestmark = pytest.mark.tempo

GAS_TARGET = 10_000_000  # per TIP-1067; a block above this raises the next base fee


async def _wait_for_block(w3, n):
    while await w3.eth.block_number < n:
        await asyncio.sleep(0.2)


async def test_base_fee_rises_on_congestion_and_decays_when_idle(w3, chain_id):
    senders = [new_account() for _ in range(6)]
    for s in senders:
        await fund(w3, s.address)

    start = await w3.eth.block_number
    # each transfer creates a new account (~250k gas under TIP-1000), so a packed block
    # blows past the 10M target; sending concurrently lands several in the same 1s block
    await asyncio.gather(
        *[
            send_calls(
                w3,
                chain_id=chain_id,
                private_key=s.key.hex(),
                gas_limit=STATE_WRITE_GAS,
                calls=[transfer_call(new_account().address, 1) for _ in range(30)],
            )
            for s in senders
        ]
    )

    await _wait_for_block(w3, start + 12)
    blocks = [await w3.eth.get_block(start + i) for i in range(12)]
    fee = [b["baseFeePerGas"] for b in blocks]
    gas = [b["gasUsed"] for b in blocks]

    # a congested block (gas over target) raises the following block's base fee
    assert any(gas[i] > GAS_TARGET and fee[i + 1] > fee[i] for i in range(len(blocks) - 1)), (gas, fee)
    # an idle block (no gas) decays the following block's base fee
    assert any(gas[i] == 0 and fee[i + 1] < fee[i] for i in range(len(blocks) - 1)), (gas, fee)
