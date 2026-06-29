"""EVM ``BALANCE`` opcode is 0 for a stablecoin-funded account."""

from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD

from .utils import deploy_contract

# Runtime returns CALLER.balance (BALANCE opcode); init copies the 10-byte runtime out.
CALLER_BALANCE_INIT = "600a600c600039600a6000f3" + "333160005260206000f3"


async def test_evm_balance_is_zero_despite_stablecoin(w3, chain_id, funded_account):
    assert await ERC20.fns.balanceOf(funded_account.address).call(w3, to=PATH_USD) > 0  # holds the stablecoin

    _, probe = await deploy_contract(
        w3, chain_id=chain_id, private_key=funded_account.key.hex(), bytecode=CALLER_BALANCE_INIT
    )
    # BALANCE(funded_account), seen from inside the EVM, is 0 — there is no native token.
    ret = await w3.eth.call({"to": probe, "from": funded_account.address})
    assert int.from_bytes(ret, "big") == 0
