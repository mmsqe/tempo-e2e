"""``python -m integration_tests.stack [--bridged-nvnm]``: the bridge, staking and a fee router on a
running dev node and anvil, relayed until Ctrl-C; every address lands in an env file.

``--staking ADDRESS`` builds only the bridge, around a staking that is already there."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from eth_account import Account
from tempo.constants import PATH_USD
from web3 import AsyncWeb3

from .abi import FEE_ROUTER_FACTORY, STAKING
from .anvil import ALICE_KEY, DEPLOYER_KEY, MILLION, RELAYER_KEY
from .bridge import DEPLOYER, Bridge, deploy, give_nvnm, relay
from .staking import Staking, fee_router
from .utils import connect, cs, fund, new_account

# Two candidates for one seat, readable since the election returns them.
VALIDATOR, OTHER = "0x" + "11" * 20, "0x" + "22" * 20
COMMISSION_BPS = 1_000
# NVNM devnet and testnet. The bridge's owner and its one DVN are anvil's public keys, so an L1
# anyone can reach must never get it: whoever grants it mint rights hands them to everyone.
PUBLIC_CHAIN_IDS = {787222, 787223}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


async def _around(eth, l1, args) -> tuple[Bridge, dict]:
    """The bridge alone around `--staking`. Names say which chain, so the file can be sourced
    beside that staking's own variables."""
    for key in (DEPLOYER_KEY, RELAYER_KEY):
        await fund(l1, Account.from_key(key).address)
    stake, reward = [
        cs(await fn.call(l1, to=args.staking)) for fn in (STAKING.fns.stakeToken(), STAKING.fns.rewardToken())
    ]
    print(f"deploying LayerZero and both gateways around {args.staking}", flush=True)
    b = await deploy(eth, l1, staking=Staking(l1, await l1.eth.chain_id, args.staking, stake, reward))
    return b, {
        "ETH_RPC": args.eth_rpc,
        "L1_RPC": args.l1_rpc,
        "DEPLOYER_KEY": DEPLOYER_KEY,
        "ETH_NVNM": b.nvnm,
        "ETH_LOCKBOX": b.lockbox,
        "LOCK_GATEWAY": b.lock_gateway,
        "L1_TOKEN": b.token,
        "MINT_GATEWAY": b.mint_gateway,
    }


async def _up(eth, l1, args) -> tuple[Bridge, dict]:
    alice = Account.from_key(ALICE_KEY)
    # L1 gas, and LayerZero's fee there, are pathUSD from the faucet.
    for key in (DEPLOYER_KEY, ALICE_KEY, RELAYER_KEY):
        await fund(l1, Account.from_key(key).address)
    print("deploying LayerZero, both gateways and staking", flush=True)
    b = await deploy(eth, l1, tip20=not args.bridged_nvnm)
    await give_nvnm(b, alice.address, MILLION)

    print("deploying a fee router and the election", flush=True)
    # Fresh each run, so a rerun on the same chain starts their balances at zero.
    operator, treasury = (new_account().address for _ in range(2))
    factory, router, lockbox = await fee_router(b.staking, DEPLOYER, VALIDATOR, operator, COMMISSION_BPS, treasury)
    # A second's unbonding keeps a manual unstake quick.
    await b.staking.setup_election(DEPLOYER, [VALIDATOR, OTHER], seats=1, unbonding=1)

    burn_spender, _ = b.burn_approval(MILLION)
    return b, {
        "ETH_RPC": args.eth_rpc,
        "L1_RPC": args.l1_rpc,
        "DEPLOYER": DEPLOYER.address,
        "DEPLOYER_KEY": DEPLOYER_KEY,
        "ALICE": alice.address,
        "ALICE_KEY": ALICE_KEY,
        "AMOUNT": MILLION,
        "UNITS": b.units(MILLION),
        "NVNM": b.nvnm,
        "LOCKBOX": b.lockbox,
        "LOCK_GATEWAY": b.lock_gateway,
        "L1_TOKEN": b.token,
        "HELD": b.held,
        "MINT_GATEWAY": b.mint_gateway,
        "BURN_SPENDER": burn_spender,
        "STAKING": b.staking.address,
        "FACTORY": factory,
        "ROUTER": router,
        "FEE_LOCKBOX": lockbox,
        "VALIDATOR": VALIDATOR,
        "OTHER": OTHER,
        "OPERATOR": operator,
        "TREASURY": treasury,
        "BUYBACKS": await FEE_ROUTER_FACTORY.fns.BUYBACK_SINK().call(l1, to=factory),
        "PATH_USD": PATH_USD,
    }


async def _run(args) -> None:
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    eth = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(args.eth_rpc))
    l1 = connect(args.l1_rpc)
    try:
        # The deployer is anvil's first account, funded on an anvil and nowhere else.
        client = (await eth.provider.make_request("web3_clientVersion", []))["result"]
        if not client.startswith("anvil"):
            raise SystemExit(f"{args.eth_rpc} is not an anvil ({client})")
        # Said in a line, before anything is deployed: the L1 is elsewhere, or staking not there yet.
        if urlparse(args.l1_rpc).hostname not in LOCAL_HOSTS:
            raise SystemExit(f"{args.l1_rpc} is not on this machine; the bridge's keys are anvil's, public")
        try:
            chain_id = await l1.eth.chain_id
        except OSError:
            raise SystemExit(f"no node answers at {args.l1_rpc}; pass --l1-rpc") from None
        if chain_id in PUBLIC_CHAIN_IDS:  # a tunnel to a public node is still a public node
            raise SystemExit(f"{args.l1_rpc} is NVNM chain {chain_id}; the bridge's keys are anvil's, public")
        if args.staking and not await l1.eth.get_code(cs(args.staking)):
            raise SystemExit(f"nothing is deployed at {args.staking} on {args.l1_rpc}")
        b, env = await (_around if args.staking else _up)(eth, l1, args)
        args.env_file.write_text("".join(f"export {k}={v}\n" for k, v in env.items()))
        print(f"ready, relaying; source {args.env_file}", flush=True)
        await asyncio.gather(relay(b.eth, b.l1), relay(b.l1, b.eth))
    finally:
        await eth.provider.disconnect()
        await l1.provider.disconnect()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stack", description="Stand the bridge and staking up to walk by hand.")
    parser.add_argument("--eth-rpc", default="http://127.0.0.1:8546", help="the anvil standing in for Ethereum")
    parser.add_argument("--l1-rpc", default="http://127.0.0.1:8545", help="the tempo dev node")
    parser.add_argument("--bridged-nvnm", action="store_true", help="mint BridgedNVNM instead of a TIP-20")
    parser.add_argument("--staking", help="a staking that exists, over a TIP-20: build only the bridge around it")
    parser.add_argument("--env-file", type=Path, default=Path("/tmp/nvnm-stack.env"), help="where the addresses go")
    try:
        asyncio.run(_run(parser.parse_args(argv)))
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
