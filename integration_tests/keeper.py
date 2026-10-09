"""Fee keeper: for each router, pay out what the chain collected for it and split it.

    ROUTERS=0xabc,0xdef RPC=https://… KEEPER_KEY=0x… python -m integration_tests.keeper

Run on a timer. Both calls are permissionless, so the key needs only gas.
"""

import asyncio
import os

from eth_account import Account
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD

from .abi import FEE, FEE_ROUTER
from .utils import STATE_WRITE_GAS, connect, cs, send_calls


async def keep(w3, chain_id, signer, router):
    """One router's turn: pay out what the chain collected for it, then split it."""
    router = cs(router)
    # The token the chain credits this router in: the pool's reward token, or PATH_USD if unset.
    token = cs(await FEE.fns.validatorTokens(router).call(w3, to=FEE_MANAGER_ADDRESS))
    if int(token, 16) == 0:
        token = cs(PATH_USD)
    collected = await FEE.fns.collectedFees(router, token).call(w3, to=FEE_MANAGER_ADDRESS)

    # One 0x76 per router: `flush` reads what `distributeFees` just moved, and a revert cannot take
    # another router's payout with it.
    calls = [{"to": router, "data": FEE_ROUTER.fns.flush(token).data}]
    if collected:
        calls.insert(0, {"to": FEE_MANAGER_ADDRESS, "data": FEE.fns.distributeFees(router, token).data})
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=signer.key.hex(), calls=calls, gas_limit=STATE_WRITE_GAS
    )
    assert receipt["status"] == 1, f"keeper tx reverted for {router}"
    print(f"{router} collected={collected} token={token}")

    # Wants a human, not a retry: the factory owner has to sweep and convert what was escrowed.
    reward = cs(await FEE_ROUTER.fns.rewardToken().call(w3, to=router))
    if token != reward:
        held = await FEE_ROUTER.fns.heldForDelegators(token).call(w3, to=router)
        print(f"  WARN fee token is not the pool's {reward}; escrowed {held}")
    return collected


async def _main():
    missing = [name for name in ("RPC", "ROUTERS", "KEEPER_KEY") if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)}")
    w3 = connect(os.environ["RPC"])
    chain_id, signer = await w3.eth.chain_id, Account.from_key(os.environ["KEEPER_KEY"])
    for router in os.environ["ROUTERS"].split(","):
        await keep(w3, chain_id, signer, router)


if __name__ == "__main__":
    asyncio.run(_main())
