"""Driving the staking stack: the deployer, the pools, routers and their addresses.

Shared because two suites stand it up on different chains -- the dev node for the
contracts' own behaviour, a consensus localnet for the epoch feed that reads them --
and the keeper imports it rather than shelling out.
"""

import json
from pathlib import Path

import rlp
from eth_abi.abi import encode
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import PATH_USD, VALIDATOR_CONFIG_V2_ADDRESS
from web3 import Web3

from .abi import (
    FEE_ROUTER_FACTORY,
    MOCK_ERC20,
    STAKING,
    STAKING_DEPLOYER,
    VALIDATOR_CONFIG_V2,
)
from .utils import STATE_WRITE_GAS, new_account, send_calls

ETHER = 10**18
cs = Web3.to_checksum_address

# Custom-error selector (keccak256(sig)[:4]) for revert-reason assertions.
ERR_AMOUNT_TOO_LARGE = "0x" + keccak(text="AmountTooLarge()")[:4].hex()

# FeeRouterFactory.RouterCreated, for picking it out of a batch that also logs the factory's own
# constructor events.
ROUTER_CREATED_TOPIC = keccak(text="RouterCreated(address,address,address,uint256)")

# The global fee pool's sentinel "validator": every staker delegates here and every validator's
# router deposits here, so the pool key belongs to no real operator.
GLOBAL_POOL = cs(keccak(b"nvm.global.pool")[12:])


def _bytecode(name):
    return json.loads((Path(__file__).parent / "artifacts" / f"{name}.json").read_text())["deployer_bytecode"]


DEPLOYER_BYTECODE = _bytecode("staking")
FACTORY_BYTECODE = _bytecode("feerouter_factory")
ROUTER_BYTECODE = _bytecode("feerouter")  # for predicting the factory's CREATE2 address
POOL_BYTECODE = _bytecode("swap_pool")
MOCK_ERC20_BYTECODE = _bytecode("mock_erc20")
BRIDGED_NVNM_BYTECODE = _bytecode("bridged_nvnm")
GUARDED_SWAPPER_BYTECODE = _bytecode("guarded_swapper")


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

    async def setup_election(self, owner, validators):
        """Every validator a candidate, under a 21-seat committee with no weight or cap."""
        for v in validators:
            await self.send(owner, STAKING.fns.setCandidate(v, True))
        await self.send(owner, STAKING.fns.setCommitteeConfig(21, 1, 0))

    async def elected(self, **kwargs):
        """``computeCommittee()`` as checksummed validators, ready to compare directly."""
        vals = await self.call(STAKING.fns.computeCommittee(), **kwargs)
        return [cs(a) for a in vals]


async def deploy(w3, chain_id, deployer, reward_token=None):
    """Deploy via StakingDeployer; ``deployer`` owns and is pre-funded.

    A zero reward token makes the deployer CREATE a second ERC-20 in the same tx as
    the staking impl + proxy. That extra create reverts on a tempo dev node (state-gas
    / create budget), so the mock USD is deployed first and passed in.
    """
    if reward_token is None:
        reward_token = await mock_token(w3, chain_id, deployer, "nvmnUSD")
        await transact(w3, chain_id, deployer, reward_token, MOCK_ERC20.fns.mint(deployer.address, 1_000 * ETHER))
    arg = encode(["address"], [reward_token]).hex()
    d = await create(w3, chain_id, deployer, DEPLOYER_BYTECODE + arg)
    addr, nvnm, usd = (
        cs(await STAKING_DEPLOYER.fns.staking().call(w3, to=d)),
        cs(await STAKING_DEPLOYER.fns.nvnm().call(w3, to=d)),
        cs(await STAKING_DEPLOYER.fns.usd().call(w3, to=d)),
    )
    return Staking(w3, chain_id, addr, nvnm, usd)


async def router_setup(w3, chain_id, owner, commission_bps, *, split=False):
    """PATH_USD staking, a factory over it (commission cap 100%), and one router.

    `split` applies the Phase 1 protocol cuts (25 devshare / 25 buybacks) so a test can assert
    the whole waterfall; the two cut recipients come back either way.
    """
    staking = await deploy(w3, chain_id, owner, reward_token=PATH_USD)
    validator, operator = new_account().address, new_account().address
    arg = encode(["address", "address", "uint256"], [staking.address, owner.address, 10_000]).hex()
    factory = await create(w3, chain_id, owner, FACTORY_BYTECODE + arg)

    receipt = await staking.send(owner, FEE_ROUTER_FACTORY.fns.create(validator, operator, commission_bps), to=factory)
    log = next(lg for lg in receipt["logs"] if bytes(lg["topics"][0]) == ROUTER_CREATED_TOPIC)
    router = cs(bytes(log["data"])[12:32])

    treasury, buybacks = new_account().address, new_account().address
    if split:
        split_fn = FEE_ROUTER_FACTORY.fns.setProtocolSplit(treasury, buybacks, 2_500, 2_500)
        await staking.send(owner, split_fn, to=factory)
    return staking, validator, operator, router, treasury, buybacks


async def mock_token(w3, chain_id, deployer, symbol):
    arg = encode(["string", "string"], [symbol, symbol]).hex()
    return await create(w3, chain_id, deployer, MOCK_ERC20_BYTECODE + arg)


# Routers cost ~4.5M gas each to deploy, against a 30M per-tx cap; three fit beside the factory.
ROUTERS_PER_TX = 3


def create_address(sender: str, nonce: int) -> str:
    """The address of a CREATE from ``sender`` at ``nonce``."""
    return cs(keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[12:])


# StakingDeployer's CREATEs, from a contract's starting nonce of 1: mock NVNM, staking impl,
# ERC-1967 proxy. Only holds when a real reward token is passed — a zero one mints a mock USD
# too and shifts these by one. test_committee_follows_staking_election asserts the deployed
# proxy against this, so a drifting sequence fails there rather than silently.
_STAKING_NVNM_NONCE = 1
STAKING_PROXY_NONCE = 3


def _staking_deploys_from(sender: str, nonce: int) -> tuple[str, str]:
    """The (mock NVNM, staking proxy) a StakingDeployer created at ``sender``'s ``nonce`` makes."""
    deployer = create_address(sender, nonce)
    return create_address(deployer, _STAKING_NVNM_NONCE), create_address(deployer, STAKING_PROXY_NONCE)


def _router_address(factory: str, validator: str, operator: str, commission_bps: int, staking: str) -> str:
    """The CREATE2 address `FeeRouterFactory.create` will deploy to.

    Lets the create and the calls that need its address ride in one tx. The salt and
    constructor args mirror the factory; a drift shows up as the RouterCreated log in that
    same receipt disagreeing, which the caller asserts.
    """
    salt = keccak(encode(["address", "address", "uint256"], [validator, operator, commission_bps]))
    initcode = bytes.fromhex(ROUTER_BYTECODE.removeprefix("0x")) + encode(
        ["address", "address", "address", "address", "uint256"],
        [validator, operator, staking, factory, commission_bps],
    )
    return cs(keccak(b"\xff" + bytes.fromhex(factory[2:]) + salt + keccak(initcode))[12:])


async def deploy_stack(w3, chain_id, deployer, *, routers, commission_bps, stake, split=None, validators=None):
    """Stand the fee stack up in three txs: one router per operator, all keyed to `GLOBAL_POOL`.

    Every delegator share then lands in the one pool a staker holds shares in, whichever
    validator earned it. `split` applies the protocol cuts; `validators` repoints a registry's
    fee recipients at the routers, one each.

    Three is a floor, not a choice: a 0x76 takes at most one CREATE and only as its first call,
    so StakingDeployer and the factory cannot share a tx, and the 30M gas cap fits only three
    routers at ~4.5M each. Nothing waits on a receipt — CREATE and CREATE2 addresses are both
    predictable, so the wiring batches with the deploys.
    """
    nonce = await w3.eth.get_transaction_count(deployer.address)
    nvnm, staking = _staking_deploys_from(deployer.address, nonce)
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

    # tx 1 — StakingDeployer: the mock NVNM, the impl, the proxy, and a stake balance for us.
    await send([{"to": None, "data": DEPLOYER_BYTECODE + encode(["address"], [PATH_USD]).hex()}])

    creates = [
        {"to": factory, "data": FEE_ROUTER_FACTORY.fns.create(GLOBAL_POOL, p, commission_bps).data} for p in payouts
    ]
    factory_create = {
        "to": None,
        "data": FACTORY_BYTECODE + encode(["address", "address", "uint256"], [staking, deployer.address, 10_000]).hex(),
    }

    # tx 2 — the factory, plus as many routers as fit beside it.
    first = await send([factory_create, *creates[:ROUTERS_PER_TX]])
    # tx 3 — the rest of the routers, then the wiring, which is all cheap calls.
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
            {"to": nvnm, "data": ERC20.fns.approve(staking, stake).data},
            {"to": staking, "data": STAKING.fns.stake(GLOBAL_POOL, stake).data},
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
