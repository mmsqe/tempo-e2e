"""TIP-1004 permit (EIP-2612): an offline 712 approval; the domain is {name(), "1", chainId, token}."""

import pytest
from eth_abi import encode
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import PATH_USD

from .abi import TIP20_PERMIT as PERMIT
from .utils import STATE_WRITE_GAS, call_revert, funded, latest_timestamp, new_account, send_call, send_calls

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


async def _fresh_permit(w3, chain_id, owner, spender, value, *, key=None, v_shift=0, deadline_offset=3600):
    """Permit calldata over ``owner``'s current nonce, valid until ``deadline_offset`` seconds from now."""
    nonce = await PERMIT.fns.nonces(owner.address).call(w3, to=PATH_USD)
    name = await PERMIT.fns.name().call(w3, to=PATH_USD)
    deadline = await latest_timestamp(w3) + deadline_offset
    signed = Account.sign_typed_data(
        key if key is not None else owner.key,  # `key` forges a signature from someone other than `owner`
        domain_data={"name": name, "version": "1", "chainId": chain_id, "verifyingContract": PATH_USD},
        message_types=PERMIT_TYPES,
        message_data={"owner": owner.address, "spender": spender, "value": value, "nonce": nonce, "deadline": deadline},
    )
    r, s = signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")
    return PERMIT.fns.permit(owner.address, spender, value, deadline, signed.v + v_shift, r, s).data


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
    owner, spender = await funded(w3), await funded(w3)  # spender relays the tx and pays its gas
    recipient = new_account().address

    value = 1000
    nonce = await PERMIT.fns.nonces(owner.address).call(w3, to=PATH_USD)
    permit = await _fresh_permit(w3, chain_id, owner, spender.address, value)

    owner_before = await ERC20.fns.balanceOf(owner.address).call(w3, to=PATH_USD)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=spender.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": PATH_USD, "data": permit},
            {"to": PATH_USD, "data": ERC20.fns.transferFrom(owner.address, recipient, value).data},
        ],
    )
    assert receipt["status"] == 1
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == value
    assert await ERC20.fns.balanceOf(owner.address).call(w3, to=PATH_USD) == owner_before - value
    assert await PERMIT.fns.nonces(owner.address).call(w3, to=PATH_USD) == nonce + 1  # nonce consumed once


async def test_expired_permit_reverts(w3, chain_id):
    owner = await funded(w3)
    spender = new_account().address
    data = await _fresh_permit(w3, chain_id, owner, spender, 1000, deadline_offset=-1)  # already past
    reason = await call_revert(w3, PATH_USD, data)
    assert "PermitExpired" in reason or "0x1a15a3cc" in reason


async def test_permit_wrong_signer_reverts(w3, chain_id):
    owner, stranger = await funded(w3), new_account()
    spender = new_account().address
    # a permit naming `owner` but signed by someone else recovers the wrong address
    data = await _fresh_permit(w3, chain_id, owner, spender, 1000, key=stranger.key)
    assert "InvalidSignature" in await call_revert(w3, PATH_USD, data)


async def test_permit_v_not_normalized(w3, chain_id):
    owner = await funded(w3)
    spender = new_account().address
    # tempo requires v in {27, 28} and does NOT normalize {0, 1} (many signing libs emit the latter)
    data = await _fresh_permit(w3, chain_id, owner, spender, 1000, v_shift=-27)
    assert "InvalidSignature" in await call_revert(w3, PATH_USD, data)


async def test_permit_replay_and_zero_override(w3, chain_id):
    owner = await funded(w3)
    spender = new_account().address

    data = await _fresh_permit(w3, chain_id, owner, spender, 1000)
    await send_call(w3, chain_id, owner, PATH_USD, data)  # owner relays its own permit
    assert await ERC20.fns.allowance(owner.address, spender).call(w3, to=PATH_USD) == 1000

    # replaying the same permit fails: the nonce moved, so recovery no longer matches the owner
    assert "InvalidSignature" in await call_revert(w3, PATH_USD, data)

    # a fresh permit with value=0 overrides the live allowance down to zero
    await send_call(w3, chain_id, owner, PATH_USD, await _fresh_permit(w3, chain_id, owner, spender, 0))
    assert await ERC20.fns.allowance(owner.address, spender).call(w3, to=PATH_USD) == 0
