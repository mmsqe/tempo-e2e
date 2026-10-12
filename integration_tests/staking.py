"""Stands the staking stack up -- deployer, pools, routers -- for the dev-node suite, the
consensus localnet and the keeper alike."""

import json
import os
from pathlib import Path
from typing import NamedTuple

import rlp
from eth_abi.abi import encode
from eth_account import Account
from eth_contract.create3 import CREATEX, CREATEX_FACTORY, create3_address
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import PATH_USD, TIP20_FACTORY_ADDRESS, VALIDATOR_CONFIG_V2_ADDRESS
from web3 import AsyncWeb3

from .abi import (
    FEE_ROUTER_FACTORY,
    STAKING,
    STAKING_DEPLOYER,
    TIP20_FACTORY,
    TIP20_ROLES,
    VALIDATOR_CONFIG_V2,
)
from .network import FAUCET_PRIVATE_KEY
from .utils import (
    ISSUER_ROLE,
    call_revert,
    create_token,
    cs,
    new_account,
    send_call,
    send_calls,
    wait_for_timestamp,
)

ETHER = 10**18
DAY = 86_400


def bytecode(name):
    return json.loads((Path(__file__).parent / "artifacts" / f"{name}.json").read_text())["deployer_bytecode"]


DEPLOYER_BYTECODE = bytecode("staking")
FACTORY_BYTECODE = bytecode("feerouter_factory")
ROUTER_BYTECODE = bytecode("feerouter")  # for predicting the factory's CREATE2 address
LOCKBOX_BYTECODE = bytecode("fee_lockbox")
POOL_BYTECODE = bytecode("swap_pool")
BRIDGED_NVNM_BYTECODE = bytecode("bridged_nvnm")
GUARDED_SWAPPER_BYTECODE = bytecode("guarded_swapper")

# The Phase 1 split, devshare and buybacks, that every lockbox starts on.
SPLIT_BPS = (2_500, 2_500)
# Seconds a split proposal is public before it may apply: short, so a test can wait it out.
SPLIT_DELAY = 2


def deployer_initcode(owner, reward_token, stake_token) -> str:
    """StakingDeployer's initcode over two existing tokens."""
    return DEPLOYER_BYTECODE + encode(["address", "address", "address"], [owner, stake_token, reward_token]).hex()


def lockbox_initcode(owner, split=SPLIT_BPS) -> str:
    return LOCKBOX_BYTECODE + encode(["address", "uint256", "uint256", "uint256"], [owner, *split, SPLIT_DELAY]).hex()


def factory_initcode(staking, lockbox, owner, treasury, max_commission=10_000) -> str:
    """A factory paying devshare to `treasury`; buybacks go to its fixed sink."""
    args = encode(
        ["address", "address", "address", "uint256", "address"],
        [staking, lockbox, owner, max_commission, treasury],
    )
    return FACTORY_BYTECODE + args.hex()


async def tip20(w3, chain_id, admin, name, *, currency="USD", amount=1_000 * ETHER) -> str:
    """A fresh TIP-20 from the factory, `amount` of it minted to `admin`."""
    salt = os.urandom(32)  # the session's node outlives any one test's token
    mint = (admin.address, amount)
    return await create_token(w3, chain_id=chain_id, admin=admin, name=name, currency=currency, mint=mint, salt=salt)


class Staking(NamedTuple):
    w3: AsyncWeb3
    chain_id: int
    address: str
    nvnm: str  # the stake token
    usd: str  # the reward token

    async def send(self, signer, fn, to=None):
        return await send_call(self.w3, self.chain_id, signer, to or self.address, fn.data)

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
        await wait_for_timestamp(self.w3, finish)

    async def setup_election(self, owner, validators, *, seats=21, unbonding=DAY):
        """Every validator a candidate under a ``seats`` committee with no weight or cap, after the
        unbonding period an election requires."""
        await self.send(owner, STAKING.fns.setUnbondingPeriod(unbonding))
        for v in validators:
            await self.send(owner, STAKING.fns.setCandidate(v, True))
        await self.send(owner, STAKING.fns.setCommitteeConfig(seats, 1, 0))

    async def elected(self, eligible=None, **kwargs):
        """``computeCommittee`` as checksummed validators, ready to compare directly. ``eligible``
        stands in for the node's registry; by default every candidate is in it."""
        if eligible is None:
            eligible = await self.call(STAKING.fns.candidates(), **kwargs)
        vals = await self.call(STAKING.fns.computeCommittee(eligible), **kwargs)
        return [cs(a) for a in vals]


def create3_salt(deployer: str, tag: bytes | None = None) -> bytes:
    """A CreateX salt only `deployer` can use: its address, a zero byte, then `tag`, random if none."""
    tag = os.urandom(11) if tag is None else tag.ljust(11, b"\x00")
    return bytes.fromhex(deployer[2:]) + b"\x00" + tag


def create3_call(salt: bytes, initcode: str) -> dict:
    """A call deploying `initcode` by CREATE3: the address follows the sender and `salt` alone."""
    data = CREATEX.fns.deployCreate3(salt, bytes.fromhex(initcode.removeprefix("0x"))).data
    return {"to": CREATEX_FACTORY, "data": data}


async def create3(w3, chain_id, deployer, initcode: str, salt: bytes | None = None) -> str:
    """Deploy `initcode` by CREATE3, under a fresh salt if none is given, and return its address."""
    salt = salt or create3_salt(deployer.address)
    call = create3_call(salt, initcode)
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=deployer.key.hex(), calls=[call], gas_limit=25_000_000
    )
    if receipt["status"] != 1:
        # CreateX says only FailedContractCreation; a bare create of the initcode says why.
        raise AssertionError(f"create3 reverted: {await call_revert(w3, None, initcode, sender=deployer.address)}")
    return create3_address(salt, deployer=deployer.address)


STAKING_PROXY_NONCE = 2  # StakingDeployer's CREATEs from nonce 1: impl, proxy


def staking_address(deployer: str, salt: bytes) -> str:
    """The staking proxy `deploy` makes for `deployer` under `salt`."""
    d = create3_address(salt, deployer=deployer)
    return cs(keccak(rlp.encode([bytes.fromhex(d[2:]), STAKING_PROXY_NONCE]))[12:])


# A localnet's genesis names the staking proxy before it exists: the faucet's, under this salt.
_FAUCET = Account.from_key(FAUCET_PRIVATE_KEY).address
STAKING_SALT = create3_salt(_FAUCET, b"staking")
STAKING_GENESIS_ADDRESS = staking_address(_FAUCET, STAKING_SALT)


async def deploy(w3, chain_id, deployer, reward_token=None, stake_token=None, salt=None):
    """Deploy via StakingDeployer, owned by ``deployer``. A token not given is a fresh TIP-20 with
    1,000 minted to ``deployer``. ``salt`` fixes the proxy at its `staking_address`."""
    reward_token = reward_token or await tip20(w3, chain_id, deployer, "nUSD")
    stake_token = stake_token or await tip20(w3, chain_id, deployer, "NVNM", currency="NVNM")
    d = await create3(w3, chain_id, deployer, deployer_initcode(deployer.address, reward_token, stake_token), salt)
    addr = cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d))
    staking = Staking(w3, chain_id, addr, cs(stake_token), cs(reward_token))
    # Callers read rewards seconds after a deposit, not the default day.
    await staking.send(deployer, STAKING.fns.setRewardDuration(1))
    return staking


async def fee_router(staking, owner, validator, operator, commission_bps, treasury) -> tuple[str, str, str]:
    """A lockbox and a factory over `staking`, cutting 25% devshare to `treasury` and 25% buybacks,
    and one router for `validator`, as (factory, router, lockbox)."""
    w3, chain_id, me = staking.w3, staking.chain_id, owner.address
    lockbox = await create3(w3, chain_id, owner, lockbox_initcode(me))
    factory = await create3(w3, chain_id, owner, factory_initcode(staking.address, lockbox, me, treasury))
    receipt = await staking.send(owner, FEE_ROUTER_FACTORY.fns.create(validator, operator, commission_bps), to=factory)
    return factory, created_routers(receipt)[0], lockbox


def created_routers(receipt) -> list[str]:
    return [cs(e["args"]["router"]) for e in FEE_ROUTER_FACTORY.events.RouterCreated.parse_logs(receipt["logs"])]


class Routed(NamedTuple):
    staking: Staking
    validator: str
    operator: str
    router: str
    treasury: str
    lockbox: str


async def router_setup(w3, chain_id, owner, commission_bps) -> Routed:
    """PATH_USD staking, a lockbox and factory over it on the 25/25 split, and one router."""
    staking = await deploy(w3, chain_id, owner, reward_token=PATH_USD)
    validator, operator = new_account().address, new_account().address
    treasury = new_account().address
    _, router, lockbox = await fee_router(staking, owner, validator, operator, commission_bps, treasury)
    return Routed(staking, validator, operator, router, treasury, lockbox)


# Routers cost ~4.5M gas each to deploy, against a 30M per-tx cap; three fit beside the factory.
ROUTERS_PER_TX = 3


def _router_address(factory: str, validator: str, operator: str, commission_bps: int, staking: str) -> str:
    """The CREATE2 address ``FeeRouterFactory.create`` will use."""
    salt = keccak(encode(["address", "address", "uint256"], [validator, operator, commission_bps]))
    initcode = bytes.fromhex(ROUTER_BYTECODE.removeprefix("0x")) + encode(
        ["address", "address", "address", "address", "uint256"],
        [validator, operator, staking, factory, commission_bps],
    )
    return cs(keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + keccak(initcode))[12:])


async def deploy_stack(
    w3, chain_id, deployer, *, routers, commission_bps, stake, treasury, validators=None, reward_token=PATH_USD
):
    """The fee stack in four 0x76 txs via predicted addresses: one router per validator, each
    feeding that validator's pool, which takes `stake`. `validators` are registry entries to
    repoint at their routers, else fresh addresses. Returns (staking, routers, pools, payouts,
    lockbox)."""
    me = deployer.address
    salt = os.urandom(32)  # the NVNM TIP-20 is created in tx 4, after tx 1 names it
    nvnm = cs(await TIP20_FACTORY.fns.getTokenAddress(me, salt).call(w3, to=TIP20_FACTORY_ADDRESS))
    staking_salt, lockbox_salt, factory_salt = (create3_salt(me) for _ in range(3))
    staking = staking_address(me, staking_salt)
    lockbox = create3_address(lockbox_salt, deployer=me)
    factory = create3_address(factory_salt, deployer=me)
    payouts = [new_account().address for _ in range(routers)]
    pools = [cs(v[1]) for v in validators] if validators else [new_account().address for _ in payouts]
    addresses = [_router_address(factory, v, p, commission_bps, staking) for v, p in zip(pools, payouts, strict=True)]
    assert len(addresses) <= 2 * ROUTERS_PER_TX, "more routers than this four-tx shape can deploy"

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
    await send([create3_call(staking_salt, deployer_initcode(me, reward_token, nvnm))])
    # tx 2 — the lockbox, which the factory names and every router reads as it is built.
    await send([create3_call(lockbox_salt, lockbox_initcode(me))])

    creates = [
        {"to": factory, "data": FEE_ROUTER_FACTORY.fns.create(v, p, commission_bps).data}
        for v, p in zip(pools, payouts, strict=True)
    ]
    factory_create = create3_call(factory_salt, factory_initcode(staking, lockbox, me, treasury))

    # tx 3 — the factory, plus as many routers as fit beside it.
    first = await send([factory_create, *creates[:ROUTERS_PER_TX]])
    # tx 4 — the rest of the routers, then the wiring, the NVNM and our stake, which are all calls.
    create_nvnm = TIP20_FACTORY.fns.createToken("NVNM", "NVNM", "NVNM", PATH_USD, deployer.address, salt)
    rest = await send(
        [
            *creates[ROUTERS_PER_TX:],
            *(
                {"to": VALIDATOR_CONFIG_V2_ADDRESS, "data": VALIDATOR_CONFIG_V2.fns.setFeeRecipient(v[5], r).data}
                for v, r in zip(validators or [], addresses, strict=bool(validators))
            ),
            {"to": TIP20_FACTORY_ADDRESS, "data": create_nvnm.data},
            {"to": nvnm, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, deployer.address).data},
            {"to": nvnm, "data": ERC20.fns.mint(deployer.address, stake * len(pools)).data},
            {"to": nvnm, "data": ERC20.fns.approve(staking, stake * len(pools)).data},
            *({"to": staking, "data": STAKING.fns.stake(v, stake).data} for v in pools),
            {"to": staking, "data": STAKING.fns.setRewardDuration(1).data},  # as `deploy` does
        ]
    )

    assert created_routers(first) + created_routers(rest) == addresses, "predicted router addresses drifted"
    return Staking(w3, chain_id, staking, nvnm, cs(reward_token)), addresses, pools, payouts, lockbox
