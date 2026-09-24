"""Unmodified type-2 tx pays gas in default stablecoin."""

import pytest
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import new_account, send_type_2

pytestmark = pytest.mark.tempo


async def test_type2_transfer_pays_gas_in_stablecoin(w3, funded_account):
    recipient = new_account().address
    receipt = await send_type_2(w3, funded_account, PATH_USD, ERC20.fns.transfer(recipient, 4321).data)
    assert receipt["type"] == 2  # a plain EIP-1559 tx, not the 0x76 AA type
    assert await ERC20.fns.balanceOf(recipient).call(w3, to=PATH_USD) == 4321
