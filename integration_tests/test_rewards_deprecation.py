"""TIP-1075: T7 makes TIP-20 rewards silent no-ops; T8 removes the machinery from the transfer path."""

import pytest
from eth_contract.erc20 import ERC20

from .abi import TIP20_REWARDS as REW
from .utils import create_token, new_account, rpc_result, send_call

pytestmark = pytest.mark.tempo

# Between a holder-to-holder transfer's 54,658 gas at T7 and 39,858 at T8, which drops the reward hook.
NO_REWARD_HOOK_GAS = 45_000


async def test_reward_opt_in_and_distribute_are_noops(w3, chain_id, funded_account):
    admin = funded_account
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 1_000_000))
    assert await REW.fns.globalRewardPerToken().call(w3, to=token) == 0
    assert await REW.fns.optedInSupply().call(w3, to=token) == 0

    # setRewardRecipient is a no-op success: the recipient is never recorded
    await send_call(w3, chain_id, admin, token, REW.fns.setRewardRecipient(admin.address).data)
    recipient, _, _ = await REW.fns.userRewardInfo(admin.address).call(w3, to=token)
    assert int(recipient, 16) == 0  # unchanged from the zero default
    assert await REW.fns.optedInSupply().call(w3, to=token) == 0

    # distributeReward is a no-op success (pre-T7 this reverted NoOptedInSupply); no tokens pulled
    before = await ERC20.fns.balanceOf(admin.address).call(w3, to=token)
    await send_call(w3, chain_id, admin, token, REW.fns.distributeReward(1000).data)
    assert await ERC20.fns.balanceOf(admin.address).call(w3, to=token) == before
    assert await REW.fns.globalRewardPerToken().call(w3, to=token) == 0


async def test_transfer_accrues_no_rewards(w3, chain_id, funded_account):
    admin = funded_account
    bob = new_account().address
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 1_000_000))

    await send_call(w3, chain_id, admin, token, ERC20.fns.transfer(bob, 1000).data)
    assert await REW.fns.getPendingRewards(admin.address).call(w3, to=token) == 0
    assert await REW.fns.getPendingRewards(bob).call(w3, to=token) == 0
    assert await REW.fns.globalRewardPerToken().call(w3, to=token) == 0
    assert await REW.fns.optedInSupply().call(w3, to=token) == 0


async def test_claim_rewards_pays_nothing_and_does_not_opt_in(w3, chain_id, funded_account):
    """T8: nothing accrues, so claiming pays zero, and it no longer opts the caller in."""
    admin = funded_account
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 1_000_000))

    before = await ERC20.fns.balanceOf(admin.address).call(w3, to=token)
    await send_call(w3, chain_id, admin, token, REW.fns.claimRewards().data)

    assert await ERC20.fns.balanceOf(admin.address).call(w3, to=token) == before  # nothing paid out
    recipient, reward_per_token, reward_balance = await REW.fns.userRewardInfo(admin.address).call(w3, to=token)
    assert (int(recipient, 16), reward_per_token, reward_balance) == (0, 0, 0)
    assert await REW.fns.optedInSupply().call(w3, to=token) == 0  # claiming is not an opt-in


async def test_transfer_no_longer_runs_the_reward_hook(w3, chain_id, funded_account):
    """With the accumulator gone, a transfer writes only the two balance slots."""
    admin = funded_account
    bob = new_account().address
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 1_000_000))

    await send_call(w3, chain_id, admin, token, ERC20.fns.transfer(bob, 1000).data)  # creates bob's balance slot
    receipt = await send_call(w3, chain_id, admin, token, ERC20.fns.transfer(bob, 1000).data)
    assert receipt["gasUsed"] < NO_REWARD_HOOK_GAS

    trace = await rpc_result(
        w3,
        "debug_traceTransaction",
        [receipt["transactionHash"].hex(), {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}}],
    )
    written = trace["post"][token.lower()]["storage"]
    balances = {
        await ERC20.fns.balanceOf(admin.address).call(w3, to=token),
        await ERC20.fns.balanceOf(bob).call(w3, to=token),
    }
    assert {int(value, 16) for value in written.values()} == balances
    assert len(written) == 2, f"transfer wrote {len(written)} slots on the token, expected 2 balances"
