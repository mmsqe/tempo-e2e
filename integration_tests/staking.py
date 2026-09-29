"""Stands the staking stack up -- deployer, pools, routers -- for the dev-node suite, the
consensus localnet and the keeper alike."""

import asyncio
import json
import os
from pathlib import Path

import rlp
from eth_abi.abi import encode
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import PATH_USD, TIP20_FACTORY_ADDRESS, VALIDATOR_CONFIG_V2_ADDRESS
from web3 import Web3

from .abi import (
    FEE_ROUTER_FACTORY,
    STAKING,
    STAKING_DEPLOYER,
    TIP20_FACTORY,
    TIP20_ROLES,
    VALIDATOR_CONFIG_V2,
)
from .utils import ISSUER_ROLE, STATE_WRITE_GAS, create_token, new_account, send_calls

ETHER = 10**18
DAY = 86_400
cs = Web3.to_checksum_address

# FeeRouterFactory.RouterCreated, for picking it out of a batch that also logs the factory's own
# constructor events.
ROUTER_CREATED_TOPIC = keccak(text="RouterCreated(address,address,address,uint256)")

# The global fee pool's sentinel "validator": every staker delegates here and every validator's
# router deposits here, so the pool key belongs to no real operator.
GLOBAL_POOL = cs(keccak(b"nvm.global.pool")[12:])


def bytecode(name):
    return json.loads((Path(__file__).parent / "artifacts" / f"{name}.json").read_text())["deployer_bytecode"]


DEPLOYER_BYTECODE = bytecode("staking")
FACTORY_BYTECODE = bytecode("feerouter_factory")
ROUTER_BYTECODE = bytecode("feerouter")  # for predicting the factory's CREATE2 address
POOL_BYTECODE = bytecode("swap_pool")
BRIDGED_NVNM_BYTECODE = bytecode("bridged_nvnm")
GUARDED_SWAPPER_BYTECODE = bytecode("guarded_swapper")


def deployer_initcode(reward_token, stake_token) -> str:
    """StakingDeployer's initcode over two existing tokens."""
    return DEPLOYER_BYTECODE + encode(["address", "address"], [stake_token, reward_token]).hex()


async def tip20(w3, chain_id, admin, name, *, currency="USD", amount=1_000 * ETHER) -> str:
    """A fresh TIP-20 from the factory, `amount` of it minted to `admin`."""
    salt = os.urandom(32)  # the session's node outlives any one test's token
    mint = (admin.address, amount)
    return await create_token(w3, chain_id=chain_id, admin=admin, name=name, currency=currency, mint=mint, salt=salt)


async def transact(w3, chain_id, signer, to, fn, expect=1):
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        calls=[{"to": to, "data": fn.data}],
        gas_limit=STATE_WRITE_GAS,
    )
    assert receipt["status"] == expect
    return receipt


async def create(w3, chain_id, deployer, data):
    """Send a create tx and return the new contract's address."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=deployer.key.hex(),
        calls=[{"to": None, "data": data}],
        gas_limit=25_000_000,
    )
    if receipt["status"] != 1:
        # Create has no `to`; eth_call the initcode from the deployer to surface the revert.
        resp = await w3.provider.make_request(
            "eth_call",
            [{"from": deployer.address, "data": data}, "latest"],
        )
        err = resp.get("error") or {}
        why = f"{err.get('message', '')} {err.get('data', '') or ''}".strip() or receipt
        raise AssertionError(f"create reverted: {why}")
    return cs(receipt["contractAddress"])


class Staking:
    def __init__(self, w3, chain_id, address, nvnm, usd):
        self.w3, self.chain_id = w3, chain_id
        self.address, self.nvnm, self.usd = address, nvnm, usd

    async def send(self, signer, fn, to=None):
        return await transact(self.w3, self.chain_id, signer, to or self.address, fn)

    async def call(self, fn, to=None, **kwargs):
        return await fn.call(self.w3, to=to or self.address, **kwargs)

    async def balance(self, token, who):
        return await self.call(ERC20.fns.balanceOf(who), to=token)

    async def transfer(self, signer, token, to, amount):
        await self.send(signer, ERC20.fns.transfer(to, amount), to=token)

    async def earned(self, validator, user):
        return await self.call(STAKING.fns.earned(validator, user))

    async def staked_of(self, validator, user):
        return await self.call(STAKING.fns.stakedOf(validator, user))

    async def stake(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.nvnm)
        await self.send(signer, STAKING.fns.stake(validator, amount))

    async def deposit_reward(self, signer, validator, amount):
        await self.send(signer, ERC20.fns.approve(self.address, amount), to=self.usd)
        await self.send(signer, STAKING.fns.depositReward(validator, amount))

    async def vest(self, validator):
        """Wait out ``validator``'s reward stream, so ``earned`` reads every deposit whole."""
        _, finish = await self.call(STAKING.fns.rewardStream(validator))
        while (await self.w3.eth.get_block("latest"))["timestamp"] < finish:
            await asyncio.sleep(0.2)

    async def setup_election(self, owner, validators):
        """Every validator a candidate, under a 21-seat committee with no weight or cap, and the
        day's unbonding an election requires."""
        await self.send(owner, STAKING.fns.setUnbondingPeriod(DAY))
        for v in validators:
            await self.send(owner, STAKING.fns.setCandidate(v, True))
        await self.send(owner, STAKING.fns.setCommitteeConfig(21, 1, 0))

    async def elected(self, eligible=None, **kwargs):
        """``computeCommittee`` as checksummed validators, ready to compare directly. ``eligible``
        stands in for the node's registry; by default every candidate is in it."""
        if eligible is None:
            eligible = await self.call(STAKING.fns.candidates(), **kwargs)
        vals = await self.call(STAKING.fns.computeCommittee(eligible), **kwargs)
        return [cs(a) for a in vals]


async def deploy(w3, chain_id, deployer, reward_token=None, stake_token=None):
    """Deploy via StakingDeployer, owned by ``deployer``. A token not given is a fresh TIP-20 with
    1,000 minted to ``deployer``."""
    reward_token = reward_token or await tip20(w3, chain_id, deployer, "nUSD")
    stake_token = stake_token or await tip20(w3, chain_id, deployer, "NVNM", currency="NVNM")
    d = await create(w3, chain_id, deployer, deployer_initcode(reward_token, stake_token))
    addr = cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d))
    staking = Staking(w3, chain_id, addr, cs(stake_token), cs(reward_token))
    # Callers read rewards seconds after a deposit, not the default day.
    await staking.send(deployer, STAKING.fns.setRewardDuration(1))
    return staking


async def fee_router(staking, owner, validator, operator, commission_bps, split=None) -> tuple[str, str]:
    """A factory over `staking` and one router for `validator`, as (factory, router). `split`, a
    (treasury, buybacks) pair, applies the 25/25 cuts."""
    arg = encode(["address", "address", "uint256"], [staking.address, owner.address, 10_000]).hex()
    factory = await create(staking.w3, staking.chain_id, owner, FACTORY_BYTECODE + arg)
    receipt = await staking.send(owner, FEE_ROUTER_FACTORY.fns.create(validator, operator, commission_bps), to=factory)
    log = next(lg for lg in receipt["logs"] if bytes(lg["topics"][0]) == ROUTER_CREATED_TOPIC)
    if split:
        await staking.send(owner, FEE_ROUTER_FACTORY.fns.setProtocolSplit(*split, 2_500, 2_500), to=factory)
    return factory, cs(bytes(log["data"])[12:32])


async def router_setup(w3, chain_id, owner, commission_bps, *, split=False):
    """PATH_USD staking, a factory over it, and one router; `split` applies the 25/25 cuts."""
    staking = await deploy(w3, chain_id, owner, reward_token=PATH_USD)
    validator, operator = new_account().address, new_account().address
    treasury, buybacks = new_account().address, new_account().address
    split_to = (treasury, buybacks) if split else None
    _, router = await fee_router(staking, owner, validator, operator, commission_bps, split_to)
    return staking, validator, operator, router, treasury, buybacks


# Routers cost ~4.5M gas each to deploy, against a 30M per-tx cap; three fit beside the factory.
ROUTERS_PER_TX = 3


def create_address(sender: str, nonce: int) -> str:
    """The address of a CREATE from ``sender`` at ``nonce``."""
    return cs(keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[12:])


# StakingDeployer's CREATEs from nonce 1: impl, proxy.
# test_committee_follows_staking_election asserts the proxy, so a drifting sequence fails there.
STAKING_PROXY_NONCE = 2


def staking_address(sender: str, nonce: int) -> str:
    """The staking proxy a StakingDeployer created at ``sender``'s ``nonce`` makes."""
    return create_address(create_address(sender, nonce), STAKING_PROXY_NONCE)


def _router_address(factory: str, validator: str, operator: str, commission_bps: int, staking: str) -> str:
    """The CREATE2 address `FeeRouterFactory.create` will use; the caller checks it against the
    RouterCreated log, so a drift from the factory fails loudly."""
    salt = keccak(encode(["address", "address", "uint256"], [validator, operator, commission_bps]))
    initcode = bytes.fromhex(ROUTER_BYTECODE.removeprefix("0x")) + encode(
        ["address", "address", "address", "address", "uint256"],
        [validator, operator, staking, factory, commission_bps],
    )
    return cs(keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + keccak(initcode))[12:])


async def deploy_stack(w3, chain_id, deployer, *, routers, commission_bps, stake, split=None, validators=None):
    """The fee stack in three txs, one router per operator paying into `GLOBAL_POOL`: a 0x76 carries
    one CREATE, and predicted addresses let the wiring ride with the deploys."""
    nonce = await w3.eth.get_transaction_count(deployer.address)
    salt = os.urandom(32)  # the NVNM TIP-20 is created in tx 3, after tx 1 names it
    nvnm = cs(await TIP20_FACTORY.fns.getTokenAddress(deployer.address, salt).call(w3, to=TIP20_FACTORY_ADDRESS))
    staking = staking_address(deployer.address, nonce)
    factory = create_address(deployer.address, nonce + 1)
    payouts = [new_account().address for _ in range(routers)]
    addresses = [_router_address(factory, GLOBAL_POOL, p, commission_bps, staking) for p in payouts]
    assert len(addresses) <= 2 * ROUTERS_PER_TX, "more routers than this three-tx shape can deploy"

    async def send(calls):
        receipt = await send_calls(
            w3,
            chain_id=chain_id,
            private_key=deployer.key.hex(),
            calls=calls,
            gas_limit=30_000_000,
        )
        assert receipt["status"] == 1, "deploy step reverted"
        return receipt

    # tx 1 — StakingDeployer: the impl and the proxy.
    await send([{"to": None, "data": deployer_initcode(PATH_USD, nvnm)}])

    creates = [
        {"to": factory, "data": FEE_ROUTER_FACTORY.fns.create(GLOBAL_POOL, p, commission_bps).data} for p in payouts
    ]
    factory_create = {
        "to": None,
        "data": FACTORY_BYTECODE + encode(["address", "address", "uint256"], [staking, deployer.address, 10_000]).hex(),
    }

    # tx 2 — the factory, plus as many routers as fit beside it.
    first = await send([factory_create, *creates[:ROUTERS_PER_TX]])
    # tx 3 — the rest of the routers, then the wiring, the NVNM and our stake, which are all calls.
    create_nvnm = TIP20_FACTORY.fns.createToken("NVNM", "NVNM", "NVNM", PATH_USD, deployer.address, salt)
    rest = await send(
        [
            *creates[ROUTERS_PER_TX:],
            *(
                [{"to": factory, "data": FEE_ROUTER_FACTORY.fns.setProtocolSplit(*split, 2_500, 2_500).data}]
                if split
                else []
            ),
            *(
                {"to": VALIDATOR_CONFIG_V2_ADDRESS, "data": VALIDATOR_CONFIG_V2.fns.setFeeRecipient(v[5], r).data}
                for v, r in zip(validators or [], addresses, strict=bool(validators))
            ),
            {"to": TIP20_FACTORY_ADDRESS, "data": create_nvnm.data},
            {"to": nvnm, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, deployer.address).data},
            {"to": nvnm, "data": ERC20.fns.mint(deployer.address, stake).data},
            {"to": nvnm, "data": ERC20.fns.approve(staking, stake).data},
            {"to": staking, "data": STAKING.fns.stake(GLOBAL_POOL, stake).data},
            {"to": staking, "data": STAKING.fns.setRewardDuration(1).data},  # as `deploy` does
        ]
    )

    # Match the topic, not the emitter: the factory's constructor logs into this batch too.
    logged = [
        cs(bytes(lg["data"])[12:32])
        for receipt in (first, rest)
        for lg in receipt["logs"]
        if bytes(lg["topics"][0]) == ROUTER_CREATED_TOPIC
    ]
    assert logged == addresses, "predicted router addresses drifted from the factory"
    return Staking(w3, chain_id, staking, nvnm, PATH_USD), addresses, payouts
