"""TIP-1034 channel reserve (T5): the payer locks a TIP-20 deposit; the payee or operator settles EIP-712
vouchers against it. open/topUp pull the deposit by TIP-1035 implicit approval, so no approve is needed."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from tempo.constants import TIP20_CHANNEL_RESERVE_ADDRESS as CR_ADDR

from .abi import TIP20_CHANNEL_RESERVE as CR
from .utils import (
    STATE_WRITE_GAS,
    ZERO_ADDRESS,
    blacklist_token,
    call_revert,
    create_token,
    funded,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo

SALT = bytes(32)


async def _voucher(w3, channel_id, cumulative, signer):
    """A voucher signature over the digest for ``cumulative``, signed by ``signer``."""
    digest = bytes(await CR.fns.getVoucherDigest(channel_id, cumulative).call(w3))
    return bytes(signer.unsafe_sign_hash(digest).signature)


async def _open(
    w3, chain_id, payer, *, payee, operator=ZERO_ADDRESS, deposit, authorized_signer=ZERO_ADDRESS, salt=SALT
):
    """Open a channel from ``payer``; return (channel_id, descriptor)."""
    data = CR.fns.open(payee, operator, PATH_USD, deposit, salt, authorized_signer).data
    receipt = await send_call(w3, chain_id, payer, CR_ADDR, data)
    # ChannelOpened data = [operator, token, authorizedSigner, salt, expiringNonceHash, deposit].
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == CR_ADDR.lower())
    channel_id = bytes(log["topics"][1])
    expiring = bytes(log["data"])[4 * 32 : 5 * 32]
    descriptor = (payer.address, payee, operator, PATH_USD, salt, authorized_signer, expiring)
    return channel_id, descriptor


async def test_open_locks_payer_deposit(w3, chain_id):
    payer = await funded(w3)
    before = await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD)

    channel_id, _ = await _open(w3, chain_id, payer, payee=new_account().address, deposit=5000)

    assert await CR.fns.getChannelState(channel_id).call(w3) == (0, 5000, 0)
    # deposit left the payer, no prior approve
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) <= before - 5000


async def test_settle_pays_payee_via_voucher(w3, chain_id):
    payer, payee = await funded(w3), new_account()
    operator = await funded(w3)  # operator relays settle and pays its gas
    channel_id, descriptor = await _open(
        w3, chain_id, payer, payee=payee.address, operator=operator.address, deposit=10000
    )

    amount = 2000
    sig = await _voucher(w3, channel_id, amount, payer)
    before = await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD)
    await send_call(w3, chain_id, operator, CR_ADDR, CR.fns.settle(descriptor, amount, sig).data)

    assert await CR.fns.getChannelState(channel_id).call(w3) == (amount, 10000, 0)
    # payee paid no gas, so it receives exactly the cumulative voucher amount
    assert await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD) == before + amount


async def test_settle_rejects_unauthorized_submitter(w3, chain_id):
    payer, stranger = await funded(w3), await funded(w3)
    channel_id, descriptor = await _open(w3, chain_id, payer, payee=new_account().address, deposit=4000)

    sig = await _voucher(w3, channel_id, 1000, payer)
    data = CR.fns.settle(descriptor, 1000, sig).data
    assert "NotPayeeOrOperator" in await call_revert(w3, CR_ADDR, data, sender=stranger.address)


async def test_topup_and_request_close(w3, chain_id):
    payer = await funded(w3)
    channel_id, descriptor = await _open(w3, chain_id, payer, payee=new_account().address, deposit=5000)

    await send_call(w3, chain_id, payer, CR_ADDR, CR.fns.topUp(descriptor, 3000).data)
    assert (await CR.fns.getChannelState(channel_id).call(w3))[1] == 8000  # deposit grew

    await send_call(w3, chain_id, payer, CR_ADDR, CR.fns.requestClose(descriptor).data)
    assert (await CR.fns.getChannelState(channel_id).call(w3))[2] > 0  # closeRequestedAt set


async def test_partial_settle_is_monotonic(w3, chain_id):
    payer, operator, payee = await funded(w3), await funded(w3), new_account()
    channel_id, descriptor = await _open(
        w3, chain_id, payer, payee=payee.address, operator=operator.address, deposit=5000
    )

    async def settle(cumulative):
        sig = await _voucher(w3, channel_id, cumulative, payer)
        await send_call(w3, chain_id, operator, CR_ADDR, CR.fns.settle(descriptor, cumulative, sig).data)

    before = await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD)
    await settle(1000)
    assert (await CR.fns.getChannelState(channel_id).call(w3))[0] == 1000
    assert await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD) == before + 1000
    await settle(2500)  # cumulative advances; payee receives only the 1500 delta
    assert (await CR.fns.getChannelState(channel_id).call(w3))[0] == 2500
    assert await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD) == before + 2500

    # a non-increasing cumulative reverts (checked before the voucher, so a dummy sig is fine)
    reason = await call_revert(w3, CR_ADDR, CR.fns.settle(descriptor, 2000, bytes(65)).data, sender=operator.address)
    assert "AmountNotIncreasing" in reason or "0x32d2c1a3" in reason


async def test_payee_close_refunds_payer_and_deletes_channel(w3, chain_id):
    payer = await funded(w3)
    payee = await funded(w3)  # payee submits close and pays its gas
    channel_id, descriptor = await _open(w3, chain_id, payer, payee=payee.address, deposit=5000)
    before = await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD)

    # captureAmount == settled (0) means no voucher is needed; close bypasses the grace period
    await send_call(w3, chain_id, payee, CR_ADDR, CR.fns.close(descriptor, 0, 0, b"").data)

    assert await CR.fns.getChannelState(channel_id).call(w3) == (0, 0, 0)  # channel deleted
    # full deposit refunded (nothing settled)
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == before + 5000


async def test_withdraw_before_grace_reverts(w3, chain_id):
    payer = await funded(w3)
    channel_id, descriptor = await _open(w3, chain_id, payer, payee=new_account().address, deposit=5000)
    await send_call(w3, chain_id, payer, CR_ADDR, CR.fns.requestClose(descriptor).data)

    # withdraw needs block.timestamp >= closeRequestedAt + CLOSE_GRACE_PERIOD (900s)
    reason = await call_revert(w3, CR_ADDR, CR.fns.withdraw(descriptor).data, sender=payer.address)
    assert "CloseNotReady" in reason or "0x02b81e29" in reason


async def test_settle_exceeding_deposit_reverts(w3, chain_id):
    payer, operator = await funded(w3), await funded(w3)
    channel_id, descriptor = await _open(
        w3, chain_id, payer, payee=new_account().address, operator=operator.address, deposit=5000
    )
    # cumulative > deposit is checked before the voucher, so a dummy signature is fine
    reason = await call_revert(w3, CR_ADDR, CR.fns.settle(descriptor, 6000, bytes(65)).data, sender=operator.address)
    assert "AmountExceedsDeposit" in reason


async def test_settle_rejects_invalid_voucher_signature(w3, chain_id):
    payer, operator, stranger = await funded(w3), await funded(w3), new_account()
    channel_id, descriptor = await _open(
        w3, chain_id, payer, payee=new_account().address, operator=operator.address, deposit=5000
    )
    # a voucher signed by someone other than the expected signer (the payer) recovers wrong
    bad_sig = await _voucher(w3, channel_id, 1000, stranger)
    reason = await call_revert(w3, CR_ADDR, CR.fns.settle(descriptor, 1000, bad_sig).data, sender=operator.address)
    assert "InvalidSignature" in reason


async def test_authorized_signer_signs_the_voucher(w3, chain_id):
    payer, operator, payee, signer = await funded(w3), await funded(w3), new_account(), new_account()
    channel_id, descriptor = await _open(
        w3,
        chain_id,
        payer,
        payee=payee.address,
        operator=operator.address,
        deposit=5000,
        authorized_signer=signer.address,
    )

    # with authorizedSigner set, the voucher must be signed by it -- not the payer
    sig = await _voucher(w3, channel_id, 2000, signer)
    before = await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD)
    await send_call(w3, chain_id, operator, CR_ADDR, CR.fns.settle(descriptor, 2000, sig).data)
    assert (await CR.fns.getChannelState(channel_id).call(w3))[0] == 2000
    assert await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD) == before + 2000

    # the payer's own signature is now rejected for this channel
    payer_sig = await _voucher(w3, channel_id, 3000, payer)
    reason = await call_revert(w3, CR_ADDR, CR.fns.settle(descriptor, 3000, payer_sig).data, sender=operator.address)
    assert "InvalidSignature" in reason


async def test_close_with_capture_pays_payee_and_refunds_payer(w3, chain_id):
    payer, payee = await funded(w3), new_account()
    operator = await funded(w3)  # operator submits close and pays its gas
    channel_id, descriptor = await _open(
        w3, chain_id, payer, payee=payee.address, operator=operator.address, deposit=5000
    )
    payer_before = await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD)
    payee_before = await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD)

    # capturing 2000 (> settled 0) requires a payer voucher over the cumulative amount
    sig = await _voucher(w3, channel_id, 2000, payer)
    await send_call(w3, chain_id, operator, CR_ADDR, CR.fns.close(descriptor, 2000, 2000, sig).data)

    assert await CR.fns.getChannelState(channel_id).call(w3) == (0, 0, 0)  # channel deleted
    # captured to the payee; deposit - capture refunded to the payer
    assert await ERC20.fns.balanceOf(payee.address).call(w3, to=PATH_USD) == payee_before + 2000
    assert await ERC20.fns.balanceOf(payer.address).call(w3, to=PATH_USD) == payer_before + 3000


async def test_multiple_open_in_one_tx(w3, chain_id):
    payer = await funded(w3)
    payee1, payee2 = new_account().address, new_account().address
    salt2 = b"\x01" + bytes(31)

    receipt = await send_calls(  # two channels opened atomically in one tx
        w3,
        chain_id=chain_id,
        private_key=payer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": CR_ADDR, "data": CR.fns.open(payee1, ZERO_ADDRESS, PATH_USD, 3000, SALT, ZERO_ADDRESS).data},
            {"to": CR_ADDR, "data": CR.fns.open(payee2, ZERO_ADDRESS, PATH_USD, 2000, salt2, ZERO_ADDRESS).data},
        ],
    )
    assert receipt["status"] == 1
    ids = [bytes(lg["topics"][1]) for lg in receipt["logs"] if lg["address"].lower() == CR_ADDR.lower()]
    assert len(ids) == 2  # one ChannelOpened per open
    states = [await CR.fns.getChannelState(cid).call(w3) for cid in ids]
    assert sorted(s[1] for s in states) == [2000, 3000]  # both deposits locked


async def test_open_respects_token_transfer_policy(w3, chain_id, funded_account):
    admin = funded_account
    payer = await funded(w3)
    blocked, allowed = new_account().address, new_account().address
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(payer.address, 20_000))

    await blacklist_token(w3, chain_id=chain_id, admin=admin, token=token, blocked=blocked)

    # opening to a policy-blocked payee fails the recipient admission check
    blocked_open = CR.fns.open(blocked, ZERO_ADDRESS, token, 5000, SALT, ZERO_ADDRESS).data
    assert "PolicyForbids" in await call_revert(w3, CR_ADDR, blocked_open, sender=payer.address)

    # ...while opening to an allowed payee on the same token succeeds
    allowed_open = CR.fns.open(allowed, ZERO_ADDRESS, token, 5000, SALT, ZERO_ADDRESS).data
    await send_call(w3, chain_id, payer, CR_ADDR, allowed_open)
