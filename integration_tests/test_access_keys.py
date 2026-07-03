"""Access keys: a delegated key authorized by a root account signs a tempo tx."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer
from tempo.constants import PATH_USD
from tempo.keychain import sign_tx_access_key

from .utils import fund, new_account, prepare_tx, send_signed, transfer_call

pytestmark = pytest.mark.tempo


async def test_admin_access_key_authorizes_transfer(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    access_key = new_account()
    recipient = new_account().address

    tx = await prepare_tx(w3, chain_id, root, [transfer_call(recipient, 1500)])
    signed = sign_tx_access_key(tx, access_key.key.hex(), Signer(root.key.hex()), is_admin=True)
    receipt = await send_signed(w3, signed)

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1500
