"""The NVNM bridge on two chains, and its shipped services. Tempo takes their plain EIP-1559 txs
and charges gas in a TIP-20, so the relayer needs funds on both chains and no privilege."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from typing import NamedTuple

from eth_abi.abi import encode
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import to_hex
from web3 import Web3

from .abi import BRIDGED_NVNM, NVNM_BRIDGE_ADAPTER, NVNM_LOCKBOX, NVNM_RELEASE_ADAPTER, NVNM_TOKEN, TIP20_ROLES
from .anvil import ATTESTOR_KEYS, DEPLOYER_KEY, RELAYER_KEY
from .network import free_port, terminate_process_group
from .staking import BRIDGED_NVNM_BYTECODE, bytecode
from .utils import ISSUER_ROLE, create_token

cs = Web3.to_checksum_address
ADMIN = Account.from_key(DEPLOYER_KEY).address  # deploys and administers both ends

LOCKBOX_BYTECODE = bytecode("lockbox")
BRIDGE_ADAPTER_BYTECODE = bytecode("bridge_adapter")
RELEASE_ADAPTER_BYTECODE = bytecode("release_adapter")
BRIDGED_TIP20_BYTECODE = bytecode("bridged_tip20")
NVNM_TOKEN_BYTECODE = bytecode("nvnm_token")
ERC1967_PROXY_BYTECODE = bytecode("erc1967_proxy")

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# BridgedNVNM's role bit for mint/burn (Solady OwnableRoles, `uint256 public constant BRIDGE = 1`).
BRIDGE_ROLE = 1

# BridgedTIP20's SCALE: NVNM's 18 decimals over a TIP-20's 6.
TIP20_SCALE = 10**12

# Blocks per eth_getLogs, far below any provider's cap, so these short chains still exercise catch-up.
LOG_WINDOW = 5

# The real NVNM and a holder to take some from, by chain (nvnm-erc20/deployments/README.md).
DEPLOYED_NVNM = {
    11155111: (
        "0x1C9E8420062B80E97812b56812d035B40dBa158A",
        "0x40e511f03Df69F35C778411c9BdD2e2CbFC6b445",
    ),
}


async def eth_send(w3, key, *, to=None, data: str | bytes = "0x", gas: int | None = None):
    """One plain EIP-1559 transaction, awaited; ``to=None`` is a create. The L1 takes these too, as
    it does from the shipped services. Gas is estimated: a create there is priced by the byte."""
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
    }
    if to is not None:
        tx["to"] = to
    tx["gas"] = gas or await w3.eth.estimate_gas(tx)
    signed = acct.sign_transaction(tx)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    assert receipt["status"] == 1, f"tx reverted: {receipt}"
    return receipt


async def eth_create(w3, key, data) -> str:
    return cs((await eth_send(w3, key, to=None, data=data))["contractAddress"])


class Bridge(NamedTuple):
    """Both ends, wired and ready: the addresses the services have to be told about."""

    nvnm: str  # the live one on a fork, else a fresh nvnm-erc20 token
    holder: str  # who to take it from
    lockbox: str
    release_adapter: str
    token: str  # what the adapter mints: BridgedNVNM, or a BridgedTIP20 over `tip20`
    adapter: str
    eth_chain_id: int
    l1_chain_id: int
    tip20: str | None = None

    @property
    def held(self) -> str:
        """What L1 holders hold, stake and approve."""
        return self.tip20 or self.token

    def units(self, amount: int) -> int:
        """`amount` NVNM in `held`'s decimals."""
        return amount // TIP20_SCALE if self.tip20 else amount

    def burn_approval(self, amount: int):
        """The approval on `held` a withdrawal needs: BridgedTIP20 pulls the TIP-20 itself."""
        if self.tip20:
            return ERC20.fns.approve(self.token, amount // TIP20_SCALE)
        return ERC20.fns.approve(self.adapter, amount)


async def deploy_nvnm(eth_w3, key) -> str:
    """nvnm-erc20's NVNMToken behind its proxy, with the whole supply and every role at `key`."""
    owner = Account.from_key(key).address
    impl = await eth_create(eth_w3, key, NVNM_TOKEN_BYTECODE)
    init = NVNM_TOKEN.fns.initialize((owner, owner, owner, owner)).data
    return await eth_create(eth_w3, key, ERC1967_PROXY_BYTECODE + encode(["address", "bytes"], [impl, init]).hex())


async def deploy_l1_token(l1_w3, *, tip20: bool) -> tuple[str, str | None]:
    """BridgedNVNM, or a BridgedTIP20 and the TIP-20 it issues, owned by the deployer."""
    if not tip20:
        return await eth_create(l1_w3, DEPLOYER_KEY, BRIDGED_NVNM_BYTECODE + encode(["address"], [ADMIN]).hex()), None
    # A random salt: the session's node outlives any one test's token.
    token = await create_token(
        l1_w3,
        chain_id=await l1_w3.eth.chain_id,
        admin=Account.from_key(DEPLOYER_KEY),
        name="NVNM",
        currency="NVNM",
        salt=os.urandom(32),
    )
    initcode = BRIDGED_TIP20_BYTECODE + encode(["address", "uint8", "address"], [token, 18, ADMIN]).hex()
    wrapper = await eth_create(l1_w3, DEPLOYER_KEY, initcode)
    await eth_send(l1_w3, DEPLOYER_KEY, to=token, data=TIP20_ROLES.fns.grantRole(ISSUER_ROLE, wrapper).data)
    return wrapper, token


async def deploy(eth_w3, l1_w3, *, threshold=2, tip20=False) -> Bridge:
    """Deploy both ends in the order their immutables force, then grant the two roles that move
    tokens: BRIDGE on the L1 token for the adapter, RELEASER on the lockbox for the release adapter."""
    eth_chain_id, l1_chain_id = await eth_w3.eth.chain_id, await l1_w3.eth.chain_id
    attestors = [Account.from_key(k).address for k in ATTESTOR_KEYS]

    nvnm, holder = DEPLOYED_NVNM.get(eth_chain_id, (None, None))
    if nvnm is None:
        nvnm, holder = await deploy_nvnm(eth_w3, DEPLOYER_KEY), ADMIN
    lockbox = await eth_create(
        eth_w3, DEPLOYER_KEY, LOCKBOX_BYTECODE + encode(["address", "address"], [nvnm, ADMIN]).hex()
    )

    token, tip20_token = await deploy_l1_token(l1_w3, tip20=tip20)
    adapter = await eth_create(
        l1_w3,
        DEPLOYER_KEY,
        BRIDGE_ADAPTER_BYTECODE
        + encode(
            ["address", "uint256", "address", "address", "uint256"],
            [token, eth_chain_id, lockbox, ADMIN, threshold],
        ).hex(),
    )
    await eth_send(l1_w3, DEPLOYER_KEY, to=token, data=BRIDGED_NVNM.fns.setRole(adapter, BRIDGE_ROLE, True).data)

    # From withdrawal 0: no Safe released one by hand here.
    release_adapter = await eth_create(
        eth_w3,
        DEPLOYER_KEY,
        RELEASE_ADAPTER_BYTECODE
        + encode(
            ["address", "uint256", "address", "uint256", "address", "uint256"],
            [lockbox, l1_chain_id, adapter, 0, ADMIN, threshold],
        ).hex(),
    )
    releaser = await NVNM_LOCKBOX.fns.RELEASER_ROLE().call(eth_w3, to=lockbox)
    await eth_send(eth_w3, DEPLOYER_KEY, to=lockbox, data=NVNM_LOCKBOX.fns.grantRole(releaser, release_adapter).data)

    eth_attestor_role = await NVNM_RELEASE_ADAPTER.fns.ATTESTOR_ROLE().call(eth_w3, to=release_adapter)
    l1_attestor_role = await NVNM_BRIDGE_ADAPTER.fns.ATTESTOR_ROLE().call(l1_w3, to=adapter)
    for attestor in attestors:
        await eth_send(
            eth_w3,
            DEPLOYER_KEY,
            to=release_adapter,
            data=NVNM_RELEASE_ADAPTER.fns.grantRole(eth_attestor_role, attestor).data,
        )
        grant = NVNM_BRIDGE_ADAPTER.fns.grantRole(l1_attestor_role, attestor)
        await eth_send(l1_w3, DEPLOYER_KEY, to=adapter, data=grant.data)

    return Bridge(
        nvnm=nvnm,
        holder=holder,
        lockbox=lockbox,
        release_adapter=release_adapter,
        token=token,
        adapter=adapter,
        eth_chain_id=eth_chain_id,
        l1_chain_id=l1_chain_id,
        tip20=tip20_token,
    )


async def give_nvnm(eth_w3, bridge: Bridge, to: str, amount: int):
    """Move `amount` NVNM from the holder to `to`, impersonating a fork's holder."""
    transfer = ERC20.fns.transfer(to, amount).data
    if bridge.holder == ADMIN:
        await eth_send(eth_w3, DEPLOYER_KEY, to=bridge.nvnm, data=transfer)
        return

    await eth_w3.provider.make_request("anvil_setBalance", [bridge.holder, hex(10**18)])
    await eth_w3.provider.make_request("anvil_impersonateAccount", [bridge.holder])
    try:
        sent = await eth_w3.provider.make_request(
            "eth_sendTransaction",
            [{"from": bridge.holder, "to": bridge.nvnm, "data": to_hex(transfer)}],
        )
        if error := sent.get("error"):
            raise AssertionError(f"could not move NVNM off {bridge.holder}: {error}")
        receipt = await eth_w3.eth.wait_for_transaction_receipt(sent["result"], timeout=60)
        assert receipt["status"] == 1, f"NVNM transfer reverted: {receipt}"
    finally:
        await eth_w3.provider.make_request("anvil_stopImpersonatingAccount", [bridge.holder])


def missing_binary(bin_dir: Path) -> str | None:
    """The first service binary absent from `bin_dir`, so a suite can skip with a reason."""
    for name in ("bridge-attestor", "bridge-relayer", "bridge-monitor"):
        if not (Path(bin_dir) / name).exists():
            return str(Path(bin_dir) / name)
    return None


class Services:
    """Attestors, relayers and monitor, each started on its own so a test can leave one out.
    Ethereum reads `latest` (anvil has no finality); the L1 reads `finalized`."""

    def __init__(self, bin_dir: Path, logs: Path, *, bridge: Bridge, eth_rpc, l1_rpc, eth_from, l1_from):
        self.bin_dir = Path(bin_dir)
        self.logs = Path(logs)
        self.bridge = bridge
        self.ports = [free_port() for _ in ATTESTOR_KEYS]
        self.common = {
            "ETH_RPC": eth_rpc,
            "L1_RPC": l1_rpc,
            "LOCKBOX": bridge.lockbox,
            "ADAPTER": bridge.adapter,
            "RELEASE_ADAPTER": bridge.release_adapter,
            "START_BLOCK": str(eth_from),
            "L1_START_BLOCK": str(l1_from),
            "POLL_SECONDS": "1",
            "LOG_WINDOW": str(LOG_WINDOW),
        }
        self.procs: dict[str, subprocess.Popen] = {}

    def _spawn(self, name: str, binary: str, env: dict[str, str]) -> "Services":
        self.logs.mkdir(parents=True, exist_ok=True)
        self.procs[name] = subprocess.Popen(
            [str(self.bin_dir / binary)],
            stdout=(self.logs / f"{name}.log").open("w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, **self.common, **env, "RUST_LOG": "info"},
        )
        return self

    def attestor(self, i: int) -> "Services":
        port = self.ports[i]
        return self._spawn(
            f"attestor-{i}",
            "bridge-attestor",
            {
                "ATTESTOR_KEY": ATTESTOR_KEYS[i],
                "ADAPTER_CHAIN_ID": str(self.bridge.l1_chain_id),
                "RELEASE_ADAPTER_CHAIN_ID": str(self.bridge.eth_chain_id),
                "BIND": f"127.0.0.1:{port}",
                "STATE": str(self.logs / f"attestor-{i}.db"),
                "ETH_FINALITY": "latest",
                "L1_FINALITY": "finalized",
            },
        )

    def relayer(self, key: str = RELAYER_KEY, name: str = "relayer") -> "Services":
        return self._spawn(name, "bridge-relayer", {"RELAYER_KEY": key, "ATTESTORS": ",".join(self.urls)})

    def start(self) -> "Services":
        """Every attestor and one relayer: the bridge as deployed."""
        for i in range(len(ATTESTOR_KEYS)):
            self.attestor(i)
        return self.relayer()

    def monitor(self) -> tuple[int, str]:
        """One `bridge-monitor --once`: its exit code (1 when L1 supply exceeds escrow), and output."""
        done = subprocess.run(
            [str(self.bin_dir / "bridge-monitor")],
            env={**os.environ, **self.common, "L1_TOKEN": self.bridge.token, "ONCE": "true", "RUST_LOG": "info"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        return done.returncode, ANSI.sub("", done.stdout + done.stderr)

    @property
    def urls(self) -> list[str]:
        return [f"http://127.0.0.1:{p}" for p in self.ports]

    def signed(self, i: int) -> int:
        """How many attestations attestor `i` serves from its own /health; 0 while not answering."""
        try:
            with urllib.request.urlopen(f"{self.urls[i]}/health", timeout=5) as response:
                return json.loads(response.read())["signed"]
        except OSError:
            return 0

    def log(self, name: str) -> str:
        return ANSI.sub("", (self.logs / f"{name}.log").read_text())

    def stop(self) -> None:
        for proc in self.procs.values():
            terminate_process_group(proc)
        self.procs = {}
