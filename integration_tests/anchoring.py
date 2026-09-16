"""The anchoring contract at 0x…0a00: code in the genesis alloc, storage loaded at block 0 by
``tempo init-from-binary-dump``, both from ``contracts/layout/``, the nvnmchain-contracts submodule."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from .abi import ANCHORING, ANCHORING_ADDRESS, MODULE_ADMIN_ADDRESS
from .network import TempoNode, default_genesis, free_port, generate_dev_genesis, resolve_tempo_bin
from .utils import deploy_contract, send_call

LAYOUT = Path(__file__).parent.parent / "contracts" / "layout"
if not LAYOUT.is_dir():
    raise RuntimeError(f"{LAYOUT} is missing: run `git submodule update --init contracts`")
RUNTIME_CODE = bytes.fromhex((LAYOUT / "anchoring.bin").read_text().strip().removeprefix("0x"))
MULTISIG_RUNTIME = bytes.fromhex((LAYOUT / "module-admin-multisig.bin").read_text().strip().removeprefix("0x"))

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


def seed_fixture(module_admin: str | None = None) -> dict[int, int]:
    """Slot => value as SeedFixture.t.sol left them, optionally with another module admin.

    The admin is the low 20 bytes of slot 3, beside the registry count.
    """
    fixture = json.loads((LAYOUT / "seed-fixture.json").read_text())
    slots = {int(slot, 16): int(value, 16) for slot, value in fixture.items()}
    if module_admin:
        slots[3] = slots[3] >> 160 << 160 | int(module_admin, 16)
    return slots


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
    multisig_owners: list[str] | None = None,
    code: bytes = RUNTIME_CODE,
) -> Path:
    """The dev genesis plus the contract's code, and ``storage`` if given, at ``ANCHORING_ADDRESS``.

    With ``multisig_owners``, the module admin multisig too, at the old chain's admin address with
    those owners in its slots 0..2, as the launch genesis places it. ``code`` stands in for an
    older release, for a test that watches a fork boundary replace it.
    """
    base = generate_dev_genesis(output_dir / "xtask", fork_times=fork_times) if fork_times else default_genesis()
    genesis = json.loads(base.read_text())
    placed = {ANCHORING_ADDRESS: _account(code, storage)}
    if multisig_owners:
        slots = {i: int(owner, 16) for i, owner in enumerate(multisig_owners)}
        placed[MODULE_ADMIN_ADDRESS] = _account(MULTISIG_RUNTIME, slots)
    for address, account in placed.items():
        key = address.lower()
        assert key not in genesis["alloc"], f"xtask's genesis already has {address}"
        genesis["alloc"][key] = account
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
def anchoring_node(base: Path, module_admin: str, *, multisig_owners: list[str] | None = None) -> Iterator[TempoNode]:
    """A dev node with the contract in genesis, the seed fixture loaded, and ``module_admin`` in place
    of the fixture's keyless one. The datadir is kept; ``-s`` prints how to resume it."""
    genesis = genesis_with_anchoring(base, multisig_owners=multisig_owners)
    load_dump(genesis, base / "node0", seed_fixture(module_admin))
    node = TempoNode(datadir=base / "node0", log_path=base / "node.log", genesis=genesis, http_port=free_port())
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
    """A contract that forwards a call to the anchoring contract, which the EOA gate refuses."""
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
