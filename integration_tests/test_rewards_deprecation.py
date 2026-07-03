"""TIP-1075 deprecate TIP-20 rewards: at T7 setRewardRecipient and distributeReward
are silent no-ops (success, no state change, no tokens pulled) and transfers no
longer accrue rewards. (T8 removes the machinery entirely.)
"""

import pytest
from eth_contract.erc20 import ERC20

from .abi import TIP20_REWARDS as REW
from .utils import create_token, new_account, send_call

pytestmark = pytest.mark.tempo


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
