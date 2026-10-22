"""TIP-1022 virtual addresses (T3+): TIP-20 credits to masterId ‖ 0xFD*10 ‖ userTag forward to the master.

Registration needs a 32-bit PoW salt, too much to grind in-test, so MASTER's is precomputed.
"""

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import to_checksum_address
from tempo.constants import ADDRESS_REGISTRY_ADDRESS as REG_ADDR
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD, STABLECOIN_DEX_ADDRESS
from tempo.constants import TIP20_CHANNEL_RESERVE_ADDRESS as CHANNEL_RESERVE

from .abi import ADDRESS_REGISTRY as REG
from .utils import approve_call, call_revert, create_token, fund, funded, new_account, send_call

pytestmark = pytest.mark.tempo


VIRTUAL_MAGIC = b"\xfd" * 10

# keccak(MASTER ‖ SALT)[:4] == 0x00000000
MASTER = Account.from_key("0x" + "11" * 32)
SALT = (0x01386356).to_bytes(32, "big")
MASTER_ID = bytes.fromhex("99d60106")


def _virtual(user_tag=b"\x00" * 6, master_id=MASTER_ID) -> str:
    return to_checksum_address(master_id + VIRTUAL_MAGIC + user_tag)


async def _ensure_master(w3, chain_id):
    """Register MASTER once; idempotent so tests share the session's single registration."""
    if int(await REG.fns.getMaster(MASTER_ID).call(w3), 16) == 0:
        await fund(w3, MASTER.address)
        await send_call(w3, chain_id, MASTER, REG_ADDR, REG.fns.registerVirtualMaster(SALT).data)


async def test_deposit_to_virtual_forwards_to_master(w3, chain_id):
    await _ensure_master(w3, chain_id)
    assert (await REG.fns.getMaster(MASTER_ID).call(w3)).lower() == MASTER.address.lower()

    virtual = _virtual()
    assert await REG.fns.isVirtualAddress(virtual).call(w3)
    assert (await REG.fns.resolveVirtualAddress(virtual).call(w3)).lower() == MASTER.address.lower()

    funder = await funded(w3)
    before = await ERC20.fns.balanceOf(MASTER.address).call(w3, to=PATH_USD)
    await send_call(w3, chain_id, funder, PATH_USD, ERC20.fns.transfer(virtual, 4000).data)
    # the deposit lands on the master; the virtual address itself never holds a balance
    assert await ERC20.fns.balanceOf(MASTER.address).call(w3, to=PATH_USD) == before + 4000
    assert await ERC20.fns.balanceOf(virtual).call(w3, to=PATH_USD) == 0


async def test_multiple_usertags_forward_to_master(w3, chain_id):
    await _ensure_master(w3, chain_id)
    funder = await funded(w3)
    before = await ERC20.fns.balanceOf(MASTER.address).call(w3, to=PATH_USD)
    for tag in (b"\x00\x00\x00\x00\x00\x01", b"\x00\x00\x00\x00\x00\x02"):
        virtual = _virtual(tag)
        await send_call(w3, chain_id, funder, PATH_USD, ERC20.fns.transfer(virtual, 1000).data)
        assert await ERC20.fns.balanceOf(virtual).call(w3, to=PATH_USD) == 0
    # every userTag under the master forwards to the same master
    assert await ERC20.fns.balanceOf(MASTER.address).call(w3, to=PATH_USD) == before + 2000


async def test_mint_and_transfer_from_forward_to_master(w3, chain_id, funded_account):
    await _ensure_master(w3, chain_id)
    admin = funded_account
    virtual = _virtual(b"\x00\x00\x00\x00\x00\x0a")
    token = await create_token(w3, chain_id=chain_id, admin=admin, mint=(admin.address, 1_000_000))
    before = await ERC20.fns.balanceOf(MASTER.address).call(w3, to=token)

    # mint to a virtual address forwards to the master
    await send_call(w3, chain_id, admin, token, ERC20.fns.mint(virtual, 3000).data)
    assert await ERC20.fns.balanceOf(MASTER.address).call(w3, to=token) == before + 3000

    # transferFrom to a virtual address forwards too
    spender = await funded(w3)
    await send_call(w3, chain_id, admin, **approve_call(spender.address, token, 2000))
    await send_call(w3, chain_id, spender, token, ERC20.fns.transferFrom(admin.address, virtual, 2000).data)
    assert await ERC20.fns.balanceOf(MASTER.address).call(w3, to=token) == before + 5000
    assert await ERC20.fns.balanceOf(virtual).call(w3, to=token) == 0


async def test_resolve_recipient(w3, chain_id):
    await _ensure_master(w3, chain_id)
    # a registered virtual resolves to its master; a plain address resolves to itself
    assert (await REG.fns.resolveRecipient(_virtual()).call(w3)).lower() == MASTER.address.lower()
    plain = new_account().address
    assert (await REG.fns.resolveRecipient(plain).call(w3)).lower() == plain.lower()
    # an unregistered virtual reverts
    unregistered = _virtual(master_id=b"\xde\xad\xbe\xef")
    assert "VirtualAddressUnregistered" in await call_revert(w3, REG_ADDR, REG.fns.resolveRecipient(unregistered).data)


async def test_decode_virtual_address(w3):
    tag = b"\x00\x00\x00\x00\x00\x07"
    is_virtual, master_id, user_tag = await REG.fns.decodeVirtualAddress(_virtual(tag)).call(w3)
    assert is_virtual and bytes(master_id) == MASTER_ID and bytes(user_tag) == tag
    # a plain EOA decodes as non-virtual
    assert not (await REG.fns.decodeVirtualAddress(new_account().address).call(w3))[0]


async def test_is_implicitly_approved(w3):
    # TIP-1035: FeeManager, DEX, and ChannelReserve pull tokens without a prior approve
    for addr in (FEE_MANAGER_ADDRESS, STABLECOIN_DEX_ADDRESS, CHANNEL_RESERVE):
        assert await REG.fns.isImplicitlyApproved(addr).call(w3)
    assert not await REG.fns.isImplicitlyApproved(new_account().address).call(w3)


async def test_master_id_collision(w3, chain_id):
    await _ensure_master(w3, chain_id)
    # re-registering the same (master, salt) collides with the existing masterId
    reason = await call_revert(w3, REG_ADDR, REG.fns.registerVirtualMaster(SALT).data, sender=MASTER.address)
    assert "MasterIdCollision" in reason


async def test_transfer_to_unregistered_virtual_reverts(w3, chain_id):
    funder = await funded(w3)  # funded so the revert is the resolve, not a balance check
    unregistered = _virtual(master_id=b"\xde\xad\xbe\xef")
    reason = await call_revert(w3, PATH_USD, ERC20.fns.transfer(unregistered, 1).data, sender=funder.address)
    assert "VirtualAddressUnregistered" in reason or "0xda56842c" in reason
