"""Fee keeper: for each router, pay out what the chain collected for it and split it.

    ROUTERS=0xabc,0xdef RPC=https://… KEEPER_KEY=0x… python -m integration_tests.keeper

Run on a timer. There is no second keeper: buybacks happen inside `flush`, and epochs, DKG and
committee election are the node's own work. Both calls are permissionless, so this key needs no
privilege — only gas.
"""

import asyncio
import os
import sys

from eth_account import Account
from tempo.constants import FEE_MANAGER_ADDRESS, PATH_USD
from web3 import AsyncWeb3, Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .abi import FEE, FEE_ROUTER
from .utils import send_calls

KEEP_GAS = 8_000_000
cs = Web3.to_checksum_address


async def keep(w3, chain_id, signer, router):
    """One router's turn: pay out what the chain collected for it, then split it."""
    router = cs(router)
    # What the chain actually credits this router in, which is the pool's reward token only
    # while `setValidatorToken` agrees with it — unset means the default. `flush(token)` covers
    # either.
    token = cs(await FEE.fns.validatorTokens(router).call(w3, to=FEE_MANAGER_ADDRESS))
    if int(token, 16) == 0:
        token = cs(PATH_USD)
    collected = await FEE.fns.collectedFees(router, token).call(w3, to=FEE_MANAGER_ADDRESS)

    # Payout and split ride in one tx: a 0x76 carries a list of calls, and `flush` reads the
    # balance `distributeFees` just moved. Per router, not per tick, so one that reverts cannot
    # take the others' payouts with it.
    calls = [{"to": router, "data": FEE_ROUTER.fns.flush(token).data}]
    if collected:
        calls.insert(0, {"to": FEE_MANAGER_ADDRESS, "data": FEE.fns.distributeFees(router, token).data})
    receipt = await send_calls(w3, chain_id=chain_id, private_key=signer.key.hex(), calls=calls, gas_limit=KEEP_GAS)
    assert receipt["status"] == 1, f"keeper tx reverted for {router}"
    print(f"{router} collected={collected} token={token}")

    # Wants a human, not a retry. One condition, not two: `flush` only escrows when the fee
    # token is not the pool's, so a mispointed `setValidatorToken` is both the cause and the
    # only way the escrow grows. A Safe has to sweep it, convert, and depositReward.
    reward = cs(await FEE_ROUTER.fns.rewardToken().call(w3, to=router))
    if token != reward:
        held = await FEE_ROUTER.fns.heldForDelegators(token).call(w3, to=router)
        print(f"  WARN fee token is not the pool's {reward}; escrowed {held}")
    return collected


async def _main():
    missing = [name for name in ("RPC", "ROUTERS", "KEEPER_KEY") if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)}")
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(os.environ["RPC"]))
    # Tempo headers carry the consensus payload in extraData, well past web3's 32-byte cap.
    # Without this, reading a receipt raises — and which block you land on decides whether it does.
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    chain_id, signer = await w3.eth.chain_id, Account.from_key(os.environ["KEEPER_KEY"])
    for router in os.environ["ROUTERS"].split(","):
        await keep(w3, chain_id, signer, router)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
