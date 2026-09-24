"""Hyperlane between anvil and the node: its core on both chains, routes over it, and its agents
in Docker. Security is a one-validator multisig ISM per direction. Nothing pays for delivery, as
tempo has no native balance: the operator's relayer carries the routes it subsidizes."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from eth_abi.abi import encode
from eth_account import Account

from .abi import HL_COLLATERAL, HL_ISM_FACTORY, HL_MAILBOX, HL_SYNTHETIC, HL_WARP
from .bridge import eth_create, eth_send
from .staking import create, transact

IMAGE = "gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0"
_ARTIFACT = json.loads((Path(__file__).parent / "artifacts" / "hyperlane.json").read_text())
BYTECODE: dict[str, str] = _ARTIFACT["deployer_bytecode"]

# anvil's dev accounts 6-8: a validator per origin chain, and the relayer. The validators only
# spend gas to announce where their signatures live; the relayer pays for every delivery.
VALIDATOR_KEYS = {
    "anvilhub": "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",
    "nvnml1": "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",
}
RELAYER_KEY = "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97"


def pad(address: str) -> bytes:
    """An address as Hyperlane's bytes32 router and recipient ids."""
    return bytes(12) + bytes.fromhex(address[2:])


class Side:
    """One chain as Hyperlane sees it. `domain` is Hyperlane's id for it, not the chain id: the
    agents know public chains by domain, so a local chain reusing Ethereum's 1 is refused."""

    def __init__(self, name: str, w3, *, domain: int, chain_id: int, rpc_url: str, key: str, tempo: bool):
        self.name, self.w3, self.domain, self.chain_id, self.tempo = name, w3, domain, chain_id, tempo
        self.account = Account.from_key(key)
        # Agents run in containers, and reach the host's loopback through this name.
        self.docker_rpc = rpc_url.replace("127.0.0.1", "host.docker.internal")

    async def create(self, code: str, args: bytes = b"") -> str:
        """Deploy initcode `code`, or the Hyperlane contract of that name."""
        data = BYTECODE.get(code, code) + args.hex()
        if self.tempo:
            return await create(self.w3, self.chain_id, self.account, data)
        return await eth_create(self.w3, self.account.key, data)

    async def send(self, to: str, fn):
        if self.tempo:
            return await transact(self.w3, self.chain_id, self.account, to, fn)
        return await eth_send(self.w3, self.account.key, to=to, data=fn.data)


class Core(NamedTuple):
    mailbox: str
    merkle_tree_hook: str
    validator_announce: str
    noop_hook: str  # also named as the IGP, which the agents require but nothing pays
    start_block: int


async def deploy_core(side: Side, *, trusts: str) -> Core:
    """Mailbox and hooks on `side`, with an ISM that accepts what `trusts` signs, alone."""
    start, owner = await side.w3.eth.block_number, side.account.address
    mailbox = await side.create("Mailbox", encode(["uint32"], [side.domain]))
    merkle = await side.create("MerkleTreeHook", encode(["address"], [mailbox]))
    noop = await side.create("PausableHook")
    announce = await side.create("ValidatorAnnounce", encode(["address"], [mailbox]))
    factory = await side.create("StaticMessageIdMultisigIsmFactory")
    deploy_ism = HL_ISM_FACTORY.fns.deploy([Account.from_key(trusts).address], 1)
    ism = await deploy_ism.call(side.w3, to=factory)  # deterministic in its validators
    await side.send(factory, deploy_ism)
    await side.send(mailbox, HL_MAILBOX.fns.initialize(owner, ism, noop, merkle))
    return Core(mailbox, merkle, announce, noop, start)


class Hyperlane(NamedTuple):
    hub: Side  # the anvil standing in for Ethereum
    l1: Side  # the tempo node
    hub_core: Core
    l1_core: Core
    agents: Agents


async def deploy_warp_route(h: Hyperlane, token: str) -> tuple[str, str]:
    """`token` escrowed on the hub, a synthetic on the L1, each enrolled as the other's router.
    Zero hook and ISM mean the Mailbox's defaults."""
    hub, l1, zero = h.hub, h.l1, "0x" + "00" * 20
    collateral = await hub.create(
        "HypERC20Collateral", encode(["address", "uint256", "uint256", "address"], [token, 1, 1, h.hub_core.mailbox])
    )
    await hub.send(collateral, HL_COLLATERAL.fns.initialize(zero, zero, hub.account.address))
    synthetic = await l1.create(
        "HypERC20", encode(["uint8", "uint256", "uint256", "address"], [18, 1, 1, h.l1_core.mailbox])
    )
    await l1.send(synthetic, HL_SYNTHETIC.fns.initialize(0, "NVNM", "NVNM", zero, zero, l1.account.address))
    await hub.send(collateral, HL_WARP.fns.enrollRemoteRouter(l1.domain, pad(synthetic)))
    await l1.send(synthetic, HL_WARP.fns.enrollRemoteRouter(hub.domain, pad(collateral)))
    return collateral, synthetic


class Agents:
    """Hyperlane's validators and relayer, in Docker, sharing one directory for checkpoints."""

    def __init__(self, workdir: Path, cores: list[tuple[Side, Core]]):
        self.workdir = Path(workdir)
        # The agents open their stores under these, and do not create the parents themselves.
        for sub in ("db", "checkpoints"):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        self.names: list[str] = []
        chains = {}
        for side, core in cores:
            chains[side.name] = {
                "name": side.name,
                "chainId": side.chain_id,
                "domainId": side.domain,
                "protocol": "ethereum",
                "technicalStack": "other",
                "rpcUrls": [{"http": side.docker_rpc}],
                "mailbox": core.mailbox,
                "merkleTreeHook": core.merkle_tree_hook,
                "validatorAnnounce": core.validator_announce,
                "interchainGasPaymaster": core.noop_hook,
                "index": {"from": core.start_block},
                "blocks": {"confirmations": 0, "reorgPeriod": 0, "estimateBlockTime": 1},
            }
        (self.workdir / "agent_config.json").write_text(json.dumps({"chains": chains}, indent=1))
        self.chains = list(chains)

    def _run(self, name: str, binary: str, env: dict[str, str]) -> None:
        container = f"hl-{name}-{time.time_ns()}"
        args = ["docker", "run", "-d", "--name", container, "-v", f"{self.workdir}:/data"]
        for k, v in {"CONFIG_FILES": "/data/agent_config.json", "RUST_LOG": "info", **env}.items():
            args += ["-e", f"{k}={v}"]
        subprocess.run([*args, IMAGE, f"./{binary}"], check=True, capture_output=True)
        self.names.append(container)

    def validator(self, origin: str) -> None:
        key = VALIDATOR_KEYS[origin]
        self._run(
            f"validator-{origin}",
            "validator",
            {
                "HYP_ORIGINCHAINNAME": origin,
                "HYP_VALIDATOR_KEY": key,
                # The same key announces its storage location on the origin chain, paying gas there.
                f"HYP_CHAINS_{origin.upper()}_SIGNER_KEY": key,
                "HYP_CHECKPOINTSYNCER_TYPE": "localStorage",
                "HYP_CHECKPOINTSYNCER_PATH": f"/data/checkpoints/{origin}",
                "HYP_DB": f"/data/db/validator-{origin}",
            },
        )

    def relayer(self, *, subsidizing: list[str]) -> None:
        """The operator's relayer: it carries, unpaid, only what the `subsidizing` routers send."""
        self._run(
            "relayer",
            "relayer",
            {
                "HYP_RELAYCHAINS": ",".join(self.chains),
                "HYP_DEFAULTSIGNER_KEY": RELAYER_KEY,
                "HYP_GASPAYMENTENFORCEMENT": '[{"type": "none"}]',
                "HYP_WHITELIST": json.dumps([{"senderAddress": subsidizing}]),
                "HYP_ALLOWLOCALCHECKPOINTSYNCERS": "true",
                "HYP_DB": "/data/db/relayer",
            },
        )

    def stop(self) -> None:
        """Remove the containers, keeping each one's log in the work directory."""
        for container in self.names:
            done = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            (self.workdir / f"{container}.log").write_text(done.stdout + done.stderr)
            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        self.names = []
