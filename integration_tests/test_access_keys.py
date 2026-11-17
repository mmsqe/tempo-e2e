"""Access keys: a delegated key authorized by a root account signs a tempo tx."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize
from tempo.constants import PATH_USD
from tempo.keychain import sign_tx_access_key

from .utils import (
    STATE_WRITE_GAS,
    build_tempo_tx,
    funded,
    latest_timestamp,
    new_account,
    prepare_tx,
    send_signed,
    suggested_max_fee,
    transfer_call,
)

pytestmark = pytest.mark.tempo


async def test_access_key_authorizes_transfer(w3, chain_id):
    root = await funded(w3)
    access_key = new_account()
    recipient = new_account().address

    tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, 1500)])
    signed = sign_tx_access_key(tx, access_key.key.hex(), Signer(root.key.hex()), is_admin=False)
    receipt = await send_signed(w3, signed)

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1500


async def test_a_key_expiring_by_valid_after_is_refused(w3, chain_id):
    """The key would be expired once the tx may run, so the pool refuses it; a second more is admitted."""
    valid_after = await latest_timestamp(w3) + 60  # inside the pool's 120 s window
    for expiry, admitted in ((valid_after, False), (valid_after + 1, True)):
        root, access_key = await funded(w3), new_account()
        tx = build_tempo_tx(
            chain_id=chain_id,
            valid_after=valid_after,
            gas_limit=STATE_WRITE_GAS,
            max_fee_per_gas=await suggested_max_fee(w3),
            calls=[transfer_call(new_account().address, 1)],
        )
        signed = sign_tx_access_key(tx, access_key.key.hex(), Signer(root.key.hex()), expiry=expiry, is_admin=False)
        resp = await w3.provider.make_request("eth_sendRawTransaction", [serialize(signed)])
        assert ("error" not in resp) == admitted, (expiry - valid_after, resp)
