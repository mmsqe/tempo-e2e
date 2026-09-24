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

from .abi import BRIDGED_NVNM, MOCK_ERC20, NVNM_BRIDGE_ADAPTER, NVNM_LOCKBOX, NVNM_RELEASE_ADAPTER
from .anvil import ATTESTOR_KEYS, DEPLOYER_KEY, RELAYER_KEY
from .network import free_port, terminate_process_group
from .staking import BRIDGED_NVNM_BYTECODE, MOCK_ERC20_BYTECODE, bytecode, create, transact

cs = Web3.to_checksum_address

LOCKBOX_BYTECODE = bytecode("lockbox")
BRIDGE_ADAPTER_BYTECODE = bytecode("bridge_adapter")
RELEASE_ADAPTER_BYTECODE = bytecode("release_adapter")

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# BridgedNVNM's role bit for mint/burn (Solady OwnableRoles, `uint256 public constant BRIDGE = 1`).
BRIDGE_ROLE = 1

# Blocks per eth_getLogs, far below any provider's cap, so these short chains still exercise catch-up.
LOG_WINDOW = 5

# The real NVNM and a holder to take some from, by chain (nvnm-erc20/deployments/README.md).
DEPLOYED_NVNM = {
    11155111: (
        "0x1C9E8420062B80E97812b56812d035B40dBa158A",
        "0x40e511f03Df69F35C778411c9BdD2e2CbFC6b445",
    ),
}


async def eth_send(w3, key, *, to=None, data: str | bytes = "0x", gas=8_000_000):
    """One EIP-1559 transaction on the Ethereum side, awaited; ``to=None`` is a create."""
    acct = Account.from_key(key)
    base = (await w3.eth.get_block("latest")).get("baseFeePerGas") or 0
    tip = 10**9
    tx = {
        "from": acct.address,
        "data": data,
        "nonce": await w3.eth.get_transaction_count(acct.address),
        "chainId": await w3.eth.chain_id,
        "gas": gas,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": base * 2 + tip,
    }
    if to is not None:
        tx["to"] = to
    signed = acct.sign_transaction(tx)
    tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    assert receipt["status"] == 1, f"ethereum tx reverted: {receipt}"
    return receipt


async def eth_create(w3, key, data) -> str:
    return cs((await eth_send(w3, key, to=None, data=data))["contractAddress"])


class Bridge(NamedTuple):
    """Both ends, wired and ready: the addresses the services have to be told about."""

    nvnm: str  # the Ethereum NVNM: the real one on a fork, else a mock
    holder: str | None  # who to take it from on a fork; None when we can mint it ourselves
    lockbox: str
    release_adapter: str
    token: str  # BridgedNVNM on the L1
    adapter: str
    eth_chain_id: int
    l1_chain_id: int


async def deploy(eth_w3, l1_w3, l1_chain_id, l1_deployer, *, threshold=2) -> Bridge:
    """Deploy both ends in the order their immutables force, then grant the two roles that move
    tokens: BRIDGE on the L1 token for the adapter, RELEASER on the lockbox for the release adapter."""
    eth_chain_id = await eth_w3.eth.chain_id
    admin = Account.from_key(DEPLOYER_KEY).address
    attestors = [Account.from_key(k).address for k in ATTESTOR_KEYS]

    nvnm, holder = DEPLOYED_NVNM.get(eth_chain_id, (None, None))
    if nvnm is None:
        arg = encode(["string", "string"], ["NVNM", "NVNM"]).hex()
        nvnm = await eth_create(eth_w3, DEPLOYER_KEY, MOCK_ERC20_BYTECODE + arg)
    lockbox = await eth_create(
        eth_w3, DEPLOYER_KEY, LOCKBOX_BYTECODE + encode(["address", "address"], [nvnm, admin]).hex()
    )

    token = await create(
        l1_w3, l1_chain_id, l1_deployer, BRIDGED_NVNM_BYTECODE + encode(["address"], [l1_deployer.address]).hex()
    )
    adapter = await create(
        l1_w3,
        l1_chain_id,
        l1_deployer,
        BRIDGE_ADAPTER_BYTECODE
        + encode(
            ["address", "uint256", "address", "address", "uint256"],
            [token, eth_chain_id, lockbox, l1_deployer.address, threshold],
        ).hex(),
    )
    await transact(l1_w3, l1_chain_id, l1_deployer, token, BRIDGED_NVNM.fns.setRole(adapter, BRIDGE_ROLE, True))

    release_adapter = await eth_create(
        eth_w3,
        DEPLOYER_KEY,
        RELEASE_ADAPTER_BYTECODE
        + encode(
            ["address", "uint256", "address", "address", "uint256"],
            [lockbox, l1_chain_id, adapter, admin, threshold],
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
        await transact(
            l1_w3, l1_chain_id, l1_deployer, adapter, NVNM_BRIDGE_ADAPTER.fns.grantRole(l1_attestor_role, attestor)
        )

    return Bridge(
        nvnm=nvnm,
        holder=holder,
        lockbox=lockbox,
        release_adapter=release_adapter,
        token=token,
        adapter=adapter,
        eth_chain_id=eth_chain_id,
        l1_chain_id=l1_chain_id,
    )


async def give_nvnm(eth_w3, bridge: Bridge, to: str, amount: int):
    """Put `amount` NVNM in `to`'s hands: mint it on a mock, or impersonate a real holder."""
    if bridge.holder is None:
        await eth_send(eth_w3, DEPLOYER_KEY, to=bridge.nvnm, data=MOCK_ERC20.fns.mint(to, amount).data)
        return

    await eth_w3.provider.make_request("anvil_setBalance", [bridge.holder, hex(10**18)])
    await eth_w3.provider.make_request("anvil_impersonateAccount", [bridge.holder])
    try:
        transfer = ERC20.fns.transfer(to, amount).data
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
