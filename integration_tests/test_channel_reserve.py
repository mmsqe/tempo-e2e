"""TIP-1034 TIP-20 channel reserve (0x4D5050…, T5): the payer locks a TIP-20
deposit; the payee/operator settles EIP-712 vouchers incrementally. open/topUp
pull the deposit via TIP-1035 implicit approval, so no ERC-20 approve is needed.
"""

import pytest
from eth_contract.erc20 import ERC20
from eth_utils import to_checksum_address
from tempo.constants import PATH_USD

from .abi import TIP20_CHANNEL_RESERVE as CR
from .utils import STATE_WRITE_GAS, fund, new_account, send_calls

pytestmark = pytest.mark.tempo

# TIP20_CHANNEL_RESERVE_ADDRESS
CR_ADDR = to_checksum_address("0x4D50500000000000000000000000000000000000")
ZERO_ADDR = "0x" + "00" * 20
SALT = bytes(32)


async def _send(w3, chain_id, signer, data):
    """Send one call to the channel reserve, paying gas in PATH_USD."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[{"to": CR_ADDR, "data": data}],
    )
    assert receipt["status"] == 1
    return receipt


async def _state(w3, channel_id):
    """(settled, deposit, closeRequestedAt) for a channel."""
    return await CR.fns.getChannelState(channel_id).call(w3, to=CR_ADDR)


async def _bal(w3, addr):
    return await ERC20.fns.balanceOf(addr).call(w3, to=PATH_USD)


async def _open(w3, chain_id, payer, *, payee, operator=ZERO_ADDR, deposit):
    """Open a channel from ``payer``; return (channel_id, descriptor)."""
    receipt = await _send(
        w3, chain_id, payer, CR.fns.open(payee, operator, PATH_USD, deposit, SALT, ZERO_ADDR).data
    )
    # ChannelOpened data = [operator, token, authorizedSigner, salt, expiringNonceHash, deposit].
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == CR_ADDR.lower())
    channel_id = bytes(log["topics"][1])
    expiring = bytes(log["data"])[4 * 32 : 5 * 32]
    descriptor = (payer.address, payee, operator, PATH_USD, SALT, ZERO_ADDR, expiring)
    return channel_id, descriptor


async def test_open_locks_payer_deposit(w3, chain_id):
    payer = new_account()
    await fund(w3, payer.address)
    before = await _bal(w3, payer.address)

    channel_id, _ = await _open(w3, chain_id, payer, payee=new_account().address, deposit=5000)

    assert await _state(w3, channel_id) == (0, 5000, 0)
    assert await _bal(w3, payer.address) <= before - 5000  # deposit left the payer, no prior approve


async def test_settle_pays_payee_via_voucher(w3, chain_id):
    payer, operator, payee = new_account(), new_account(), new_account()
    await fund(w3, payer.address)
    await fund(w3, operator.address)  # operator relays settle and pays its gas
    channel_id, descriptor = await _open(
        w3, chain_id, payer, payee=payee.address, operator=operator.address, deposit=10000
    )

    amount = 2000
    digest = bytes(await CR.fns.getVoucherDigest(channel_id, amount).call(w3, to=CR_ADDR))
    sig = bytes(payer.unsafe_sign_hash(digest).signature)

    before = await _bal(w3, payee.address)
    await _send(w3, chain_id, operator, CR.fns.settle(descriptor, amount, sig).data)

    assert await _state(w3, channel_id) == (amount, 10000, 0)
    # payee paid no gas, so it receives exactly the cumulative voucher amount
    assert await _bal(w3, payee.address) == before + amount


async def test_settle_rejects_unauthorized_submitter(w3, chain_id):
    payer, stranger = new_account(), new_account()
    await fund(w3, payer.address)
    await fund(w3, stranger.address)
    channel_id, descriptor = await _open(w3, chain_id, payer, payee=new_account().address, deposit=4000)

    digest = bytes(await CR.fns.getVoucherDigest(channel_id, 1000).call(w3, to=CR_ADDR))
    sig = bytes(payer.unsafe_sign_hash(digest).signature)
    data = "0x" + bytes(CR.fns.settle(descriptor, 1000, sig).data).hex()
    resp = await w3.provider.make_request(
        "eth_call", [{"from": stranger.address, "to": CR_ADDR, "data": data}, "latest"]
    )
    assert "NotPayeeOrOperator" in resp["error"]["message"]


async def test_topup_and_request_close(w3, chain_id):
    payer = new_account()
    await fund(w3, payer.address)
    channel_id, descriptor = await _open(w3, chain_id, payer, payee=new_account().address, deposit=5000)

    await _send(w3, chain_id, payer, CR.fns.topUp(descriptor, 3000).data)
    assert (await _state(w3, channel_id))[1] == 8000  # deposit grew

    await _send(w3, chain_id, payer, CR.fns.requestClose(descriptor).data)
    assert (await _state(w3, channel_id))[2] > 0  # closeRequestedAt set
    # withdraw is skipped: CLOSE_GRACE_PERIOD is 900s, too long for the suite.
