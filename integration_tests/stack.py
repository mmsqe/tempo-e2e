"""``python -m integration_tests.stack [--hyperlane] [--bridged-nvnm]``: the bridge, staking and a
fee router on a running dev node and anvil, with the carriers running and every address in an env
file, to walk the flow by hand. Ctrl-C stops what it started."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path

from eth_account import Account
from eth_contract.erc20 import ERC20
from tempo.constants import PATH_USD
from web3 import AsyncWeb3

from . import bridge as bridge_mod
from . import hyperlane as hl
from .abi import STAKING
from .anvil import ALICE_KEY, DEPLOYER_KEY, RELAYER_KEY
from .staking import deploy as deploy_staking
from .staking import fee_router
from .utils import fund, new_account

AMOUNT = 1_000_000 * 10**18  # what alice gets on Ethereum to bridge
# Two candidates for one seat, readable since the election returns them.
VALIDATOR, OTHER = "0x" + "11" * 20, "0x" + "22" * 20
COMMISSION_BPS = 1_000


async def _bridge(eth, l1, args, workdir: Path, stops: list) -> dict:
    """Our attestors and relayer, carrying for the adapters."""
    eth_from, l1_from = await eth.eth.block_number, await l1.eth.block_number
    b = await bridge_mod.deploy(eth, l1, tip20=not args.bridged_nvnm)
    services = bridge_mod.Services(
        args.bin_dir, workdir, bridge=b, eth_rpc=args.eth_rpc, l1_rpc=args.l1_rpc, eth_from=eth_from, l1_from=l1_from
    )
    stops.append(services.stop)
    services.start()
    await bridge_mod.give_nvnm(eth, b, Account.from_key(ALICE_KEY).address, AMOUNT)
    return {
        "NVNM": b.nvnm,
        "LOCKBOX": b.lockbox,
        "L1_TOKEN": b.token,
        "HELD": b.held,
        "LOCK_VIA": b.lockbox,
        "WITHDRAW_VIA": b.adapter,
        "ADAPTER": b.adapter,
        "RELEASE_ADAPTER": b.release_adapter,
    }


async def _hyperlane(eth, l1, args, workdir: Path, stops: list) -> dict:
    """Hyperlane's validators and the operator's relayer, carrying for our two routers."""
    h = await hl.start(eth, l1, hub_rpc=args.eth_rpc, l1_rpc=args.l1_rpc, workdir=workdir)
    stops.append(h.agents.stop)
    r = await hl.deploy_nvnm_route(h, tip20=not args.bridged_nvnm)
    h.agents.relayer(subsidizing=[r.lock_router, r.mint_router])
    transfer = ERC20.fns.transfer(Account.from_key(ALICE_KEY).address, AMOUNT)
    await bridge_mod.eth_send(eth, DEPLOYER_KEY, to=r.nvnm, data=transfer.data)
    return {
        "NVNM": r.nvnm,
        "LOCKBOX": r.lockbox,
        "L1_TOKEN": r.token,
        "HELD": r.held,
        "LOCK_VIA": r.lock_router,
        "WITHDRAW_VIA": r.mint_router,
        "LOCK_ROUTER": r.lock_router,
        "MINT_ROUTER": r.mint_router,
    }


async def _up(eth, l1, args, workdir: Path, stops: list) -> dict:
    deployer, alice = Account.from_key(DEPLOYER_KEY), Account.from_key(ALICE_KEY)
    # L1 gas is a TIP-20 from the faucet; anvil's accounts are funded at genesis.
    carriers = [hl.RELAYER_KEY, hl.VALIDATOR_KEYS["nvnml1"]] if args.hyperlane else [RELAYER_KEY]
    for key in (DEPLOYER_KEY, ALICE_KEY, *carriers):
        await fund(l1, Account.from_key(key).address)

    env = await (_hyperlane if args.hyperlane else _bridge)(eth, l1, args, workdir, stops)
    tip20 = not args.bridged_nvnm
    # BridgedTIP20 pulls the TIP-20 before it burns, so the approval goes to it, in TIP-20 units.
    env["UNITS"] = AMOUNT // bridge_mod.TIP20_SCALE if tip20 else AMOUNT
    env["BURN_SPENDER"] = env["L1_TOKEN"] if tip20 else env["WITHDRAW_VIA"]

    staking = await deploy_staking(l1, await l1.eth.chain_id, deployer, reward_token=PATH_USD, stake_token=env["HELD"])
    # Fresh each run, so a rerun on the same chain starts their balances at zero.
    operator, treasury, buybacks = (new_account().address for _ in range(3))
    factory, router = await fee_router(staking, deployer, VALIDATOR, operator, COMMISSION_BPS, (treasury, buybacks))
    for candidate in (VALIDATOR, OTHER):
        await staking.send(deployer, STAKING.fns.setCandidate(candidate, True))
    # The election requires an unbonding period; a second keeps a manual unstake quick.
    await staking.send(deployer, STAKING.fns.setUnbondingPeriod(1))
    await staking.send(deployer, STAKING.fns.setCommitteeConfig(1, 1, 0))

    return {
        "ETH_RPC": args.eth_rpc,
        "L1_RPC": args.l1_rpc,
        "DEPLOYER": deployer.address,
        "DEPLOYER_KEY": DEPLOYER_KEY,
        "ALICE": alice.address,
        "ALICE_KEY": ALICE_KEY,
        "AMOUNT": AMOUNT,
        **env,
        "STAKING": staking.address,
        "FACTORY": factory,
        "ROUTER": router,
        "VALIDATOR": VALIDATOR,
        "OTHER": OTHER,
        "OPERATOR": operator,
        "TREASURY": treasury,
        "BUYBACKS": buybacks,
        "PATH_USD": PATH_USD,
        "BRIDGE_BIN": args.bin_dir.resolve(),
    }


async def _run(args, workdir: Path) -> None:
    # The services run in their own sessions, so a closed terminal would orphan them too.
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    eth = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(args.eth_rpc))
    l1 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(args.l1_rpc))
    stops: list = [eth.provider.disconnect, l1.provider.disconnect]
    try:
        env = await _up(eth, l1, args, workdir, stops)
        args.env_file.write_text("".join(f"export {k}={v}\n" for k, v in env.items()))
        print(f"ready; source {args.env_file}\nlogs: {workdir}", flush=True)
        await asyncio.Event().wait()
    finally:
        for stop in reversed(stops):
            if asyncio.iscoroutine(done := stop()):
                await done


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="stack", description="Stand the bridge and staking up to walk by hand.")
    parser.add_argument("--eth-rpc", default="http://127.0.0.1:8546", help="the anvil standing in for Ethereum")
    parser.add_argument("--l1-rpc", default="http://127.0.0.1:8545", help="the tempo dev node")
    parser.add_argument("--hyperlane", action="store_true", help="carry over Hyperlane instead of our attestors")
    parser.add_argument("--bridged-nvnm", action="store_true", help="mint BridgedNVNM instead of a TIP-20")
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=Path(os.environ.get("BRIDGE_BIN_DIR", "bridge/services/target/debug")),
        help="the built bridge services (also $BRIDGE_BIN_DIR)",
    )
    parser.add_argument("--env-file", type=Path, default=Path("/tmp/nvnm-stack.env"), help="where the addresses go")
    args = parser.parse_args(argv)

    if args.hyperlane and (reason := hl.docker_unavailable()):
        parser.error(reason)
    if not args.hyperlane and (missing := bridge_mod.missing_binary(args.bin_dir)):
        parser.error(f"no bridge services built: {missing} (build them, or pass --bin-dir)")
    try:
        asyncio.run(_run(args, Path(tempfile.mkdtemp(prefix="nvnm-stack-"))))
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
