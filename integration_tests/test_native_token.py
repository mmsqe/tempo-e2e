"""There is no native token: the ``BALANCE`` opcode reads 0 and ``eth_getBalance`` is a stub."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import deploy_contract, new_account

pytestmark = pytest.mark.requires("tempo-native")

# Runtime returns CALLER.balance (BALANCE opcode); init copies the 10-byte runtime out.
CALLER_BALANCE_INIT = "600a600c600039600a6000f3" + "333160005260206000f3"

# What `eth_getBalance` answers instead of a balance, for every address. Mirrors
# NATIVE_BALANCE_PLACEHOLDER in tempo's crates/node/src/rpc/mod.rs.
NATIVE_BALANCE_PLACEHOLDER = int("42" * 38)


async def test_evm_balance_is_zero_despite_stablecoin(w3, chain_id, funded_account):
    assert await ERC20.fns.balanceOf(funded_account.address).call(w3, to=PATH_USD) > 0  # holds the stablecoin

    _, probe = await deploy_contract(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), bytecode=CALLER_BALANCE_INIT
    )
    # BALANCE(funded_account), seen from inside the EVM, is 0 — there is no native token.
    ret = await w3.eth.call({"to": probe, "from": funded_account.address})
    assert int.from_bytes(ret, "big") == 0


async def test_eth_get_balance_is_a_fixed_placeholder(w3, funded_account):
    """The same sentinel for everyone: nothing about the account reaches it."""
    assert await ERC20.fns.balanceOf(funded_account.address).call(w3, to=PATH_USD) > 0

    assert await w3.eth.get_balance(funded_account.address) == NATIVE_BALANCE_PLACEHOLDER
    assert await w3.eth.get_balance(new_account().address) == NATIVE_BALANCE_PLACEHOLDER  # never funded
    assert await w3.eth.get_balance(funded_account.address, "earliest") == NATIVE_BALANCE_PLACEHOLDER
