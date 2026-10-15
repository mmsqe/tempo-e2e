"""The anchoring contract at 0x…0a00: code in the genesis alloc, storage loaded at block 0 by
``tempo init-from-binary-dump``, both from ``contracts/layout/``, the nvnmchain-contracts submodule."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from eth_utils import to_checksum_address

from .abi import ANCHORING, ANCHORING_ADDRESS
from .network import TempoNode, default_genesis, free_port, generate_dev_genesis, resolve_tempo_bin
from .utils import deploy_contract, send_call

LAYOUT = Path(__file__).parent.parent / "contracts" / "layout"
if not LAYOUT.is_dir():
    raise RuntimeError(f"{LAYOUT} is missing: run `git submodule update --init contracts`")


def _code(name: str) -> bytes:
    return bytes.fromhex((LAYOUT / name).read_text().strip().removeprefix("0x"))


RUNTIME_CODE = _code("anchoring.bin")  # the implementation, as deployed at IMPLEMENTATION
INIT_CODE = _code("anchoring.init.bin")  # what a deployment sends the factory after the salt
PROXY_CODE = _code("proxy.bin")  # what T12 puts at 0x…0a00
_DEPLOYMENT = json.loads((LAYOUT / "deployment.json").read_text())
FACTORY = to_checksum_address(_DEPLOYMENT["factory"])
IMPLEMENTATION = to_checksum_address(_DEPLOYMENT["implementation"])
OWNER_SLOT = 11  # `_owner` in layout/anchoring.json, past every slot the seed writes
# tempo's T11_ANCHORING_RUNTIME_HASH: the ownerless build the testnet launched on
LAUNCH_RUNTIME_HASH = bytes.fromhex("893a2f955868c80ae17905aa36cc4f71a771731b530e72ff6cfb063f559a7c0e")
IMPLEMENTATION_SLOT = 0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC  # ERC-1967

# Go's time.Time.String() in UTC, whole seconds: how the contract writes block time.
GO_TIME = "%Y-%m-%d %H:%M:%S +0000 UTC"


class Registry(NamedTuple):
    id: int
    name: str
    description: str
    creator: str
    created_at: str
    metadata: str


class Record(NamedTuple):
    uri: str
    checksum: str
    checksum_algo: str
    metadata: str
    timestamp: str
    status: str
    record_id: int
    index: int
    is_latest: bool
    registry_id: int


class Page(NamedTuple):
    """A ``PageRequest``; limit 0 is the contract's default of 50."""

    key: bytes = b""
    offset: int = 0
    limit: int = 0
    count_total: bool = False
    reverse: bool = False


def seed_fixture() -> dict[int, int]:
    """Slot => value as SeedFixture.t.sol left them."""
    fixture = json.loads((LAYOUT / "seed-fixture.json").read_text())
    return {int(slot, 16): int(value, 16) for slot, value in fixture.items()}


def _account(code: bytes, storage: dict[int, int] | None = None) -> dict:
    """A genesis alloc entry for placed code: no balance, nonce 1 as a deployment would leave."""
    account = {"balance": "0x0", "code": "0x" + code.hex(), "nonce": "0x1"}
    if storage:
        account["storage"] = {f"0x{slot:064x}": f"0x{value:064x}" for slot, value in storage.items()}
    return account


def genesis_with_anchoring(
    output_dir: Path,
    *,
    storage: dict[int, int] | None = None,
    fork_times: dict[str, int] | None = None,
) -> Path:
    """The dev genesis plus the contract as T12 leaves it: the proxy at ``ANCHORING_ADDRESS`` over
    ``storage``, if given, and its implementation.

    What the generator itself writes is `test_genesis.py`'s subject.
    """
    base = generate_dev_genesis(output_dir / "xtask", fork_times=fork_times) if fork_times else default_genesis()
    genesis = json.loads(base.read_text())
    alloc = genesis["alloc"]
    key = ANCHORING_ADDRESS.lower()
    assert key not in alloc, f"xtask's genesis already has {key}"
    alloc[key] = _account(PROXY_CODE, {**(storage or {}), IMPLEMENTATION_SLOT: int(IMPLEMENTATION, 16)})
    alloc[IMPLEMENTATION.lower()] = _account(RUNTIME_CODE)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "genesis.json"
    path.write_text(json.dumps(genesis))
    return path


def load_dump(genesis: Path, datadir: Path, slots: dict[int, int]) -> None:
    """Init ``datadir`` and load ``slots`` into the contract through a TEMPOSB v1 dump: a 40-byte
    header (magic, version, flags, address, count), then 64-byte slot/value pairs, zeros dropped."""
    pairs = sorted((slot, value) for slot, value in slots.items() if value)
    dump = datadir.parent / "anchoring.dump"
    with open(dump, "wb") as f:
        f.write(b"TEMPOSB\0" + b"\0\1\0\0" + bytes.fromhex(ANCHORING_ADDRESS[2:]) + len(pairs).to_bytes(8, "big"))
        for slot, value in pairs:
            f.write(slot.to_bytes(32, "big") + value.to_bytes(32, "big"))
    chain = ("--chain", str(genesis), "--datadir", str(datadir))
    for args in (("init", *chain), ("init-from-binary-dump", *chain, str(dump))):
        done = subprocess.run([resolve_tempo_bin(), *args], capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            raise RuntimeError(f"tempo {args[0]} failed (exit {done.returncode}):\n{done.stderr[-2000:]}")


@contextmanager
def anchoring_node(base: Path, *, owner: str | None = None, extra_args: list[str] | None = None) -> Iterator[TempoNode]:
    """A dev node with the contract in genesis, ``owner`` named there if given, and the seed
    fixture loaded. The datadir is kept; ``-s`` prints how to resume it."""
    genesis = genesis_with_anchoring(base, storage={OWNER_SLOT: int(owner, 16)} if owner else None)
    load_dump(genesis, base / "node0", seed_fixture())
    node = TempoNode(
        datadir=base / "node0",
        log_path=base / "node.log",
        genesis=genesis,
        http_port=free_port(),
        extra_args=extra_args,
    )
    try:
        yield node.start().wait_for_rpc()
    finally:
        node.stop()
        print(f"\n  anchoring node kept: python -m integration_tests.devnode up --datadir {node.datadir}")


# Relays its calldata to the anchoring contract and returns the answer, revert data included:
# calldatacopy, call, returndatacopy, then revert or (at 0x33) return.
RELAY_RUNTIME = (
    "36 6000 6000 37  6000 6000 36 6000 6000 73{addr} 5a f1  3d 6000 6000 3e  6033 57  3d 6000 fd  5b 3d 6000 f3"
)


async def deploy_relay(w3, chain_id: int, account) -> str:
    runtime = bytes.fromhex(RELAY_RUNTIME.format(addr=ANCHORING_ADDRESS[2:]))
    size = f"60{len(runtime):02x}"
    init = bytes.fromhex(f"{size} 600c 6000 39 {size} 6000 f3") + runtime  # copy the runtime out, return it
    _, relay = await deploy_contract(w3, chain_id=chain_id, private_key=account.key.hex(), bytecode=init)
    return relay


def bech32(address: str, hrp: str = "nvnm") -> str:
    """BIP-173, written from the spec so it shares nothing with the contract's Bech32.sol."""
    acc, bits, words = 0, 0, []
    for byte in bytes.fromhex(address.removeprefix("0x")):
        acc, bits = acc << 8 | byte, bits + 8
        while bits >= 5:
            bits -= 5
            words.append(acc >> bits & 31)
    if bits:
        words.append(acc << 5 - bits & 31)
    chk = 1
    for value in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + words + [0] * 6:
        top, chk = chk >> 25, (chk & 0x1FFFFFF) << 5 ^ value
        for i, generator in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            if top >> i & 1:
                chk ^= generator
    words += [(chk ^ 1) >> 5 * (5 - i) & 31 for i in range(6)]
    return hrp + "1" + "".join("qpzry9x8gf2tvdw0s3jn54khce6mua7l"[w] for w in words)


def add_record(registry_id: int, checksum: str, uri: str = "", algo: str = "sha256") -> bytes:
    """``addRecord`` calldata. The chain fills in the timestamp, ids and ``isLatest``."""
    record = (uri or f"https://ex.test/{checksum}", checksum, algo, '{"by":"tempo-e2e"}', "", "Active")
    return ANCHORING.fns.addRecord((*record, 0, 0, False, registry_id)).data


async def registries(w3, registry_id: int = 0, page: Page = Page()) -> list[Registry]:
    rows, _ = await ANCHORING.fns.registries(registry_id, page).call(w3)
    return [Registry(*row) for row in rows]


async def registries_by_name(w3, name: str, page: Page = Page()) -> list[Registry]:
    rows, _ = await ANCHORING.fns.registriesByName(name, 1, page).call(w3)
    return [Registry(*row) for row in rows]


async def records(
    w3, registry_id: int = 0, checksum: str = "", *, record_id: int = 0, index: int = 0, page: Page = Page()
) -> list[Record]:
    rows, _ = await ANCHORING.fns.records(registry_id, checksum, record_id, index, page).call(w3)
    return [Record(*row) for row in rows]


async def new_registry(w3, chain_id: int, creator, name: str = "tempo-e2e") -> int:
    receipt = await send_call(w3, chain_id, creator, ANCHORING_ADDRESS, ANCHORING.fns.addRegistry(name, "", "").data)
    [(_, registry_id, _)] = emitted(receipt, "AddRegistry")
    return registry_id


def emitted(receipt, event: str) -> list[tuple]:
    """Each ``event`` the contract logged in ``receipt``, as a tuple of its arguments in ABI order."""
    abi = getattr(ANCHORING.events, event)
    logs = [log for log in receipt["logs"] if log["address"] == ANCHORING_ADDRESS]
    return [tuple(parsed["args"][i["name"]] for i in abi.abi["inputs"]) for parsed in abi.parse_logs(logs)]
