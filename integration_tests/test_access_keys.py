"""Access keys: a delegated key authorized by a root account signs a tempo tx.

xfail: tempo-py 0.1.0 keychain encoding is not accepted by the current node build.
"""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize
from tempo.constants import PATH_USD
from tempo.keychain import sign_tx_access_key

from .utils import build_tempo_tx, fund, get_nonce, new_account, suggested_max_fee

pytestmark = pytest.mark.tempo


@pytest.mark.xfail(reason="tempo-py 0.1.0 keychain encoding not accepted by current node build", strict=False)
async def test_admin_access_key_authorizes_transfer(w3, chain_id):
    root = new_account()
    await fund(w3, root.address)
    access_key = new_account()
    recipient = new_account().address

    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=await get_nonce(w3, root.address),
        fee_token=PATH_USD,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(recipient, 1500).data}],
    )
    signed = sign_tx_access_key(tx, access_key.key.hex(), Signer(root.key.hex()), is_admin=True)
    receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(serialize(signed)))

    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 1500
