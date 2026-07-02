"""TIP-1004 permit (EIP-2612): an owner signs an offline 712 approval so a spender
can approve+transferFrom in one tx. The 712 domain is {name(), "1", chainId, token}.
"""

import pytest
from eth_abi import encode
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import PATH_USD

from .abi import TIP20_PERMIT as PERMIT
from .utils import STATE_WRITE_GAS, call_revert, fund, latest_timestamp, new_account, send_calls

pytestmark = pytest.mark.tempo

DOMAIN_TYPEHASH = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
PERMIT_TYPES = {
    "Permit": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]
}


def _sign_permit(owner, *, name, chain_id, token, spender, value, nonce, deadline):
    signed = Account.sign_typed_data(
        owner.key,
        domain_data={"name": name, "version": "1", "chainId": chain_id, "verifyingContract": token},
        message_types=PERMIT_TYPES,
        message_data={"owner": owner.address, "spender": spender, "value": value, "nonce": nonce, "deadline": deadline},
    )
    return signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")


async def test_domain_separator_matches_onchain(w3, chain_id):
    name = await PERMIT.fns.name().call(w3, to=PATH_USD)
    local = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [DOMAIN_TYPEHASH, keccak(name.encode()), keccak(b"1"), chain_id, PATH_USD],
        )
    )
    assert bytes(await PERMIT.fns.DOMAIN_SEPARATOR().call(w3, to=PATH_USD)) == local


async def test_permit_authorizes_transfer_from(w3, chain_id):
    owner, spender = new_account(), new_account()
    await fund(w3, owner.address)
    await fund(w3, spender.address)  # spender relays the tx and pays its gas
    recipient = new_account().address

    value = 1000
    nonce = await PERMIT.fns.nonces(owner.address).call(w3, to=PATH_USD)
    name = await PERMIT.fns.name().call(w3, to=PATH_USD)
    deadline = await latest_timestamp(w3) + 3600
    v, r, s = _sign_permit(
        owner,
        name=name,
        chain_id=chain_id,
        token=PATH_USD,
        spender=spender.address,
        value=value,
        nonce=nonce,
        deadline=deadline,
    )

    owner_before = await ERC20.fns.balanceOf(owner.address).call(w3, to=PATH_USD)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=spender.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": PATH_USD, "data": PERMIT.fns.permit(owner.address, spender.address, value, deadline, v, r, s).data},
            {"to": PATH_USD, "data": ERC20.fns.transferFrom(owner.address, recipient, value).data},
        ],
    )
    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == value
    assert await ERC20.fns.balanceOf(owner.address).call(w3, to=PATH_USD) == owner_before - value
    assert await PERMIT.fns.nonces(owner.address).call(w3, to=PATH_USD) == nonce + 1  # nonce consumed once


async def test_expired_permit_reverts(w3, chain_id):
    owner = new_account()
    await fund(w3, owner.address)
    spender = new_account().address
    nonce = await PERMIT.fns.nonces(owner.address).call(w3, to=PATH_USD)
    name = await PERMIT.fns.name().call(w3, to=PATH_USD)
    deadline = await latest_timestamp(w3) - 1  # already past
    v, r, s = _sign_permit(
        owner, name=name, chain_id=chain_id, token=PATH_USD, spender=spender, value=1000, nonce=nonce, deadline=deadline
    )
    reason = await call_revert(w3, PATH_USD, PERMIT.fns.permit(owner.address, spender, 1000, deadline, v, r, s).data)
    assert "PermitExpired" in reason or "0x1a15a3cc" in reason
