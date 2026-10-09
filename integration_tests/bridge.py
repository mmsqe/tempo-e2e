"""NVNM over LayerZero between an anvil standing in for Ethereum and the tempo node: LayerZero's
endpoint and ULN302 at each end, the bridge's gateways over them, and this process as the one DVN
and executor. Tempo charges gas in a TIP-20, so every account here needs funds on both chains."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import NamedTuple

from eth_abi.abi import decode, encode
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_hex
from tempo.constants import PATH_USD
from web3 import AsyncWeb3

from .abi import (
    BRIDGED_NVNM,
    LZ_ENDPOINT,
    LZ_LOCK_GATEWAY,
    LZ_MINT_GATEWAY,
    LZ_RECEIVE_ULN,
    LZ_SEND_ULN,
    NVNM_LOCKBOX,
    NVNM_TOKEN,
    STAKING,
    TIP20_ROLES,
)
from .anvil import DEPLOYER_KEY, RELAYER_KEY
from .staking import BRIDGED_NVNM_BYTECODE, Staking, bytecode
from .staking import deploy as deploy_staking
from .utils import ISSUER_ROLE, create_token, cs

LOG = logging.getLogger(__name__)

DEPLOYER = Account.from_key(DEPLOYER_KEY)  # deploys and administers both ends
RELAYER = Account.from_key(RELAYER_KEY).address

LZ = bytecode("layerzero")  # by contract name
LOCK_GATEWAY_BYTECODE = bytecode("lz_lock_gateway")
MINT_GATEWAY_BYTECODE = bytecode("lz_mint_gateway")
WORKER_BYTECODE = bytecode("lz_worker")
LOCKBOX_BYTECODE = bytecode("lockbox")
BRIDGED_TIP20_BYTECODE = bytecode("bridged_tip20")
NVNM_TOKEN_BYTECODE = bytecode("nvnm_token")
ERC1967_PROXY_BYTECODE = bytecode("erc1967_proxy")

ETH_EID, L1_EID = 30_101, 30_999
SEIZE_SINK = "0x000000000000000000000000000000000000dEaD"
# Type-3 options carrying an lzReceive gas limit, which ULN302 requires of every send: enforced on
# each gateway for its one message type, so callers pass none.
OPTIONS = bytes.fromhex("000301001101" + (200_000).to_bytes(16, "big").hex())
MESSAGE = 1
PACKET_SENT = "0x" + keccak(text="PacketSent(bytes,bytes,address)").hex()

# BridgedNVNM's role bit for mint/burn (Solady OwnableRoles, `uint256 public constant BRIDGE = 1`).
BRIDGE_ROLE = 1
# BridgedTIP20's SCALE: NVNM's 18 decimals over a TIP-20's 6.
TIP20_SCALE = 10**12

# The real NVNM and a holder to impersonate on a fork of that chain (nvnm-erc20/deployments/README.md).
DEPLOYED_NVNM = {
    11155111: (
        "0x1C9E8420062B80E97812b56812d035B40dBa158A",
        "0x40e511f03Df69F35C778411c9BdD2e2CbFC6b445",
    ),
}


async def eth_send(w3, key, *, to=None, data: str | bytes = "0x", value: int = 0):
    """One awaited EIP-1559 tx; ``to=None`` creates. Gas is estimated: a create on the L1 is priced
    by the byte."""
    acct = Account.from_key(key)
    base = (await w3.eth.get_block("latest")).get("baseFeePerGas") or 0
    tip = 10**9
    tx = {
        "from": acct.address,
        "data": data,
        "nonce": await w3.eth.get_transaction_count(acct.address),
        "chainId": await w3.eth.chain_id,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": base * 2 + tip,
        "value": value,
    }
    if to is not None:
        tx["to"] = to
    tx["gas"] = await w3.eth.estimate_gas(tx)
    signed = acct.sign_transaction(tx)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    assert receipt["status"] == 1, f"tx reverted: {receipt}"
    return receipt


async def eth_create(w3, key, data) -> str:
    return cs((await eth_send(w3, key, to=None, data=data))["contractAddress"])


async def ethereum_nvnm(eth_w3) -> tuple[str, str]:
    """The NVNM to lock and who holds it: the live one on an anvil fork, where its holder can be
    impersonated, and nvnm-erc20's token behind its proxy, all of it the deployer's, elsewhere."""
    nvnm, holder = DEPLOYED_NVNM.get(await eth_w3.eth.chain_id, (None, None))
    anvil = (await eth_w3.provider.make_request("web3_clientVersion", []))["result"].startswith("anvil")
    if nvnm and anvil and await eth_w3.eth.get_code(nvnm):
        return nvnm, holder
    impl = await eth_create(eth_w3, DEPLOYER_KEY, NVNM_TOKEN_BYTECODE)
    init = NVNM_TOKEN.fns.initialize((DEPLOYER.address,) * 4).data
    proxy = ERC1967_PROXY_BYTECODE + encode(["address", "bytes"], [impl, init]).hex()
    return await eth_create(eth_w3, DEPLOYER_KEY, proxy), DEPLOYER.address


async def deploy_l1_token(l1_w3, *, tip20: bool, existing: str | None = None) -> tuple[str, str | None]:
    """BridgedNVNM, or a BridgedTIP20 and the TIP-20 it issues, owned by the deployer. Over an
    `existing` TIP-20 the wrapper comes without its ISSUER_ROLE, which is that token's admin's
    to grant."""
    if not (tip20 or existing):
        initcode = BRIDGED_NVNM_BYTECODE + encode(["address"], [DEPLOYER.address]).hex()
        return await eth_create(l1_w3, DEPLOYER_KEY, initcode), None
    # A random salt: the session's node outlives any one test's token.
    token = existing or await create_token(
        l1_w3, chain_id=await l1_w3.eth.chain_id, admin=DEPLOYER, name="NVNM", currency="NVNM", salt=os.urandom(32)
    )
    initcode = BRIDGED_TIP20_BYTECODE + encode(["address", "uint8", "address"], [token, 18, DEPLOYER.address]).hex()
    wrapper = await eth_create(l1_w3, DEPLOYER_KEY, initcode)
    if not existing:
        await eth_send(l1_w3, DEPLOYER_KEY, to=token, data=TIP20_ROLES.fns.grantRole(ISSUER_ROLE, wrapper).data)
    return wrapper, token


def peer(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


class Endpoint(NamedTuple):
    w3: AsyncWeb3
    eid: int
    address: str
    receive_lib: str


async def deploy_endpoint(w3, eid: int, remote_eid: int, *, worker_fee: int, fee_token: str | None = None) -> Endpoint:
    """LayerZero's endpoint, an EndpointV2Alt charging `fee_token` if given, with a ULN302 pair
    pointed at `remote_eid`: MockWorker quotes and takes each job sent, and the relayer is the one
    DVN a received packet needs."""
    if fee_token:
        args = encode(["uint32", "address", "address"], [eid, DEPLOYER.address, fee_token])
        endpoint = await eth_create(w3, DEPLOYER_KEY, LZ["EndpointV2Alt"] + args.hex())
    else:
        args = encode(["uint32", "address"], [eid, DEPLOYER.address])
        endpoint = await eth_create(w3, DEPLOYER_KEY, LZ["EndpointV2"] + args.hex())
    send_args = encode(["address", "uint256", "uint256"], [endpoint, 100_000, 100_000]).hex()
    send_lib = await eth_create(w3, DEPLOYER_KEY, LZ["SendUln302"] + send_args)
    receive_lib = await eth_create(w3, DEPLOYER_KEY, LZ["ReceiveUln302"] + encode(["address"], [endpoint]).hex())
    worker = await eth_create(w3, DEPLOYER_KEY, WORKER_BYTECODE + encode(["uint256"], [worker_fee]).hex())

    def uln(dvn):
        return [(remote_eid, (1, 1, 0, 0, [dvn], []))]

    for to, fn in (
        (endpoint, LZ_ENDPOINT.fns.registerLibrary(send_lib)),
        (endpoint, LZ_ENDPOINT.fns.registerLibrary(receive_lib)),
        (send_lib, LZ_SEND_ULN.fns.setDefaultUlnConfigs(uln(worker))),
        (send_lib, LZ_SEND_ULN.fns.setDefaultExecutorConfigs([(remote_eid, (10_000, worker))])),
        (receive_lib, LZ_RECEIVE_ULN.fns.setDefaultUlnConfigs(uln(RELAYER))),
        (endpoint, LZ_ENDPOINT.fns.setDefaultSendLibrary(remote_eid, send_lib)),
        (endpoint, LZ_ENDPOINT.fns.setDefaultReceiveLibrary(remote_eid, receive_lib, 0)),
    ):
        await eth_send(w3, DEPLOYER_KEY, to=to, data=fn.data)
    return Endpoint(w3, eid, endpoint, receive_lib)


class Bridge(NamedTuple):
    """Both gateways over their endpoints, and the staking the L1 one bonds into."""

    eth: Endpoint
    l1: Endpoint
    nvnm: str  # the live one on a fork, else a fresh nvnm-erc20 token
    holder: str  # who to take it from
    lockbox: str
    lock_gateway: str
    token: str  # what the L1 gateway mints: BridgedNVNM, or a BridgedTIP20 over `tip20`
    mint_gateway: str
    staking: Staking
    tip20: str | None = None

    @property
    def held(self) -> str:
        """What L1 holders and staking hold."""
        return self.tip20 or self.token

    def units(self, amount: int) -> int:
        """`amount` NVNM in `held`'s decimals."""
        return amount // TIP20_SCALE if self.tip20 else amount

    def burn_approval(self, amount: int) -> tuple[str, int]:
        """The spender and amount on `held` a withdrawal of `amount` needs: BridgedTIP20 pulls the
        TIP-20 itself, BridgedNVNM lets the gateway burn."""
        return (self.token, self.units(amount)) if self.tip20 else (self.mint_gateway, amount)

    async def escrow(self) -> int:
        return await NVNM_LOCKBOX.fns.totalLocked().call(self.eth.w3, to=self.lockbox)

    async def supply(self) -> int:
        """In NVNM's 18 decimals, as BridgedTIP20 reports its TIP-20's."""
        return await ERC20.fns.totalSupply().call(self.l1.w3, to=self.token)


async def deploy(eth_w3, l1_w3, *, tip20=False, staking: Staking | None = None) -> Bridge:
    """Both endpoints, the lockbox and its gateway on Ethereum, the L1 token, staking over it and
    the L1 gateway, each gateway holding its roles and naming the other. Around a `staking` that
    exists, its owner still has to grant the TIP-20's ISSUER_ROLE and call `setBondGateway`."""
    eth = await deploy_endpoint(eth_w3, ETH_EID, L1_EID, worker_fee=10**12)
    # Tempo carries no native value: its endpoint charges pathUSD, which every account pays gas in.
    l1 = await deploy_endpoint(l1_w3, L1_EID, ETH_EID, worker_fee=1_000, fee_token=PATH_USD)

    nvnm, holder = await ethereum_nvnm(eth_w3)
    lockbox_args = encode(["address", "address"], [nvnm, DEPLOYER.address]).hex()
    lockbox = await eth_create(eth_w3, DEPLOYER_KEY, LOCKBOX_BYTECODE + lockbox_args)
    lock_args = encode(["address", "address", "address", "uint32"], [eth.address, DEPLOYER.address, lockbox, L1_EID])
    lock_gateway = await eth_create(eth_w3, DEPLOYER_KEY, LOCK_GATEWAY_BYTECODE + lock_args.hex())
    for role in (NVNM_LOCKBOX.fns.RELEASER_ROLE(), NVNM_LOCKBOX.fns.GATEWAY_ROLE()):
        grant = NVNM_LOCKBOX.fns.grantRole(await role.call(eth_w3, to=lockbox), lock_gateway)
        await eth_send(eth_w3, DEPLOYER_KEY, to=lockbox, data=grant.data)

    ours = staking is None
    token, tip20_token = await deploy_l1_token(l1_w3, tip20=tip20, existing=None if ours else staking.nvnm)
    held = tip20_token or token
    if ours:
        chain_id = await l1_w3.eth.chain_id
        staking = await deploy_staking(l1_w3, chain_id, DEPLOYER, reward_token=PATH_USD, stake_token=held)
    mint_args = encode(
        ["address", "address", "address", "address", "address", "uint32"],
        [l1.address, DEPLOYER.address, token, held, staking.address, ETH_EID],
    )
    mint_gateway = await eth_create(l1_w3, DEPLOYER_KEY, MINT_GATEWAY_BYTECODE + mint_args.hex())
    await eth_send(l1_w3, DEPLOYER_KEY, to=token, data=BRIDGED_NVNM.fns.setRole(mint_gateway, BRIDGE_ROLE, True).data)
    if ours:
        await staking.send(DEPLOYER, STAKING.fns.setBondGateway(mint_gateway))

    # Each gateway names the other and takes the options its messages go with.
    for w3, gateway, abi, remote, other in (
        (eth_w3, lock_gateway, LZ_LOCK_GATEWAY, L1_EID, mint_gateway),
        (l1_w3, mint_gateway, LZ_MINT_GATEWAY, ETH_EID, lock_gateway),
    ):
        await eth_send(w3, DEPLOYER_KEY, to=gateway, data=abi.fns.setPeer(remote, peer(other)).data)
        enforce = abi.fns.setEnforcedOptions([(remote, MESSAGE, OPTIONS)])
        await eth_send(w3, DEPLOYER_KEY, to=gateway, data=enforce.data)
    return Bridge(eth, l1, nvnm, holder, lockbox, lock_gateway, token, mint_gateway, staking, tip20_token)


async def give_nvnm(b: Bridge, to: str, amount: int):
    """Move `amount` NVNM from the holder to `to`: as the deployer when it holds it, else
    impersonating a fork's holder."""
    w3, transfer = b.eth.w3, ERC20.fns.transfer(to, amount).data
    if b.holder == DEPLOYER.address:
        await eth_send(w3, DEPLOYER_KEY, to=b.nvnm, data=transfer)
        return

    await w3.provider.make_request("anvil_setBalance", [b.holder, hex(10**18)])
    await w3.provider.make_request("anvil_impersonateAccount", [b.holder])
    try:
        sent = await w3.provider.make_request(
            "eth_sendTransaction", [{"from": b.holder, "to": b.nvnm, "data": to_hex(transfer)}]
        )
        if error := sent.get("error"):
            raise AssertionError(f"could not move NVNM off {b.holder}: {error}")
        receipt = await w3.eth.wait_for_transaction_receipt(sent["result"], timeout=60)
        assert receipt["status"] == 1, f"NVNM transfer reverted: {receipt}"
    finally:
        await w3.provider.make_request("anvil_stopImpersonatingAccount", [b.holder])


async def lock(b: Bridge, key: str, amount: int, validator: str | None = None):
    """`key` locks `amount` of its NVNM on Ethereum toward `validator`, or without one as the bond
    of the validator at its own address, paying LayerZero in ETH."""
    w3 = b.eth.w3
    await eth_send(w3, key, to=b.nvnm, data=ERC20.fns.approve(b.lock_gateway, amount).data)
    fee, _ = await LZ_LOCK_GATEWAY.fns.quote().call(w3, to=b.lock_gateway)
    call = LZ_LOCK_GATEWAY.fns.lock(validator, amount) if validator else LZ_LOCK_GATEWAY.fns.bond(amount)
    await eth_send(w3, key, to=b.lock_gateway, data=call.data, value=fee)


async def deliver(dst: Endpoint, packet: bytes):
    """One packet, as the DVN and the executor take it: verified, committed, then delivered."""
    # PacketV1: version(1) nonce(8) srcEid(4) sender(32) dstEid(4) receiver(32) | guid(32) message
    header, payload = packet[:81], packet[81:]
    origin = (int.from_bytes(packet[9:13], "big"), packet[13:45], int.from_bytes(packet[1:9], "big"))
    receiver = cs("0x" + packet[61:81].hex())
    for to, fn in (
        (dst.receive_lib, LZ_RECEIVE_ULN.fns.verify(header, keccak(payload), 1)),
        (dst.receive_lib, LZ_RECEIVE_ULN.fns.commitVerification(header, keccak(payload))),
        (dst.address, LZ_ENDPOINT.fns.lzReceive(origin, receiver, payload[:32], payload[32:], b"")),
    ):
        await eth_send(dst.w3, RELAYER_KEY, to=to, data=fn.data)


async def relay(src: Endpoint, dst: Endpoint):
    """Deliver every packet `src` sends from now on to `dst`, until cancelled. One that fails is
    retried each pass, as an executor would."""
    cursor = await src.w3.eth.block_number
    delivered: set[bytes] = set()
    while True:
        await asyncio.sleep(0.3)
        try:
            head = await src.w3.eth.block_number
            if head < cursor:
                continue
            logs = await src.w3.eth.get_logs(
                {"address": src.address, "topics": [PACKET_SENT], "fromBlock": cursor, "toBlock": head}
            )
            through = True
            for log in logs:
                packet, _, _ = decode(["bytes", "bytes", "address"], log["data"])
                if (guid := packet[81:113]) in delivered:
                    continue
                try:
                    await deliver(dst, packet)
                    delivered.add(guid)
                except Exception as e:  # a revert, until the state it waits on arrives
                    through = False
                    LOG.warning("relaying %d → %d: %s", src.eid, dst.eid, e)
            # Only once every packet in the range is through, so a failed one is fetched again.
            if through:
                cursor = head + 1
        except Exception as e:  # the node itself, briefly
            LOG.warning("relaying %d → %d: %s", src.eid, dst.eid, e)
