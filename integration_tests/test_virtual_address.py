"""TIP-1022 virtual addresses (AddressRegistry 0xFDC0…, T3+): a master registers
under a PoW salt; a TIP-20 deposit to a derived address (masterId ‖ 0xFD*10 ‖ tag)
forwards to the master. Registration needs 32-bit PoW (keccak(master ‖ salt)[:4]
== 0) -- too much to grind in-test -- so a fixed key with a precomputed salt is
used (the node datadir is fresh per session, so it re-registers cleanly).
"""

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import to_checksum_address
from tempo.constants import PATH_USD

from .abi import ADDRESS_REGISTRY as REG
from .utils import call_revert, fund, new_account, send_call

pytestmark = pytest.mark.tempo

REG_ADDR = to_checksum_address("0xFDC0000000000000000000000000000000000000")
VIRTUAL_MAGIC = b"\xfd" * 10

# Precomputed for MASTER (see scratchpad grind): keccak(master ‖ salt)[:4] == 0x00000000.
MASTER = Account.from_key("0x" + "11" * 32)
SALT = (0x01386356).to_bytes(32, "big")
MASTER_ID = bytes.fromhex("99d60106")


def _virtual(user_tag=b"\x00" * 6, master_id=MASTER_ID) -> str:
    return to_checksum_address(master_id + VIRTUAL_MAGIC + user_tag)


async def _register_master(w3, chain_id):
    await fund(w3, MASTER.address)
    await send_call(w3, chain_id, MASTER, REG_ADDR, REG.fns.registerVirtualMaster(SALT).data)


async def test_deposit_to_virtual_forwards_to_master(w3, chain_id):
    await _register_master(w3, chain_id)
    assert (await REG.fns.getMaster(MASTER_ID).call(w3, to=REG_ADDR)).lower() == MASTER.address.lower()

    virtual = _virtual()
    assert await REG.fns.isVirtualAddress(virtual).call(w3, to=REG_ADDR)
    assert (await REG.fns.resolveVirtualAddress(virtual).call(w3, to=REG_ADDR)).lower() == MASTER.address.lower()

    funder = new_account()
    await fund(w3, funder.address)
    before = await ERC20.fns.balanceOf(MASTER.address).call(w3, to=PATH_USD)
    await send_call(w3, chain_id, funder, PATH_USD, ERC20.fns.transfer(virtual, 4000).data)
    # the deposit lands on the master; the virtual address itself never holds a balance
    assert await ERC20.fns.balanceOf(MASTER.address).call(w3, to=PATH_USD) == before + 4000
    assert await ERC20.fns.balanceOf(virtual).call(w3, to=PATH_USD) == 0


async def test_transfer_to_unregistered_virtual_reverts(w3, chain_id):
    funder = new_account()
    await fund(w3, funder.address)  # funded so the revert is the resolve, not a balance check
    unregistered = _virtual(master_id=b"\xde\xad\xbe\xef")
    reason = await call_revert(
        w3, PATH_USD, ERC20.fns.transfer(unregistered, 1).data, sender=funder.address
    )
    assert "VirtualAddressUnregistered" in reason or "0xda56842c" in reason
