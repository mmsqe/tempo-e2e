"""nvnmchain-anchoring's name search over the anchoring node: the seed fixture and a live registry,
found by prefix, suffix and contains. The service comes from PATH (or ``$ANCHORING_BIN``); without it
these skip."""

import asyncio
import os
import secrets
import subprocess
import time

import pytest
import requests

from .abi import ANCHORING, ANCHORING_ADDRESS
from .anchoring import GO_TIME, anchoring_node, bech32, emitted, needs_contracts, registries_by_name
from .network import _resolve_bin, free_port, terminate_process_group
from .utils import new_account, send_call

pytestmark = [pytest.mark.tempo, needs_contracts]

SEARCH_PATH = "/NVNM-Chain/nvnmchain/anchoring/v1/registries/search"


@pytest.fixture(scope="module")
def module_admin():
    return new_account()


@pytest.fixture(scope="module")
def tempo(tmp_path_factory, module_admin):
    """This module's node, in place of the session's."""
    # Its own name, so pytest's `anchoringcurrent` link stays test_anchoring.py's node.
    with anchoring_node(tmp_path_factory.mktemp("anchoring-service"), module_admin.address) as node:
        yield node


@pytest.fixture(scope="module")
def name_search(tempo, tmp_path_factory):
    """The service over the node: caught up before it answers, then polling every 0.2 s."""
    try:
        exe = _resolve_bin("nvnmchain-anchoring", "ANCHORING_BIN")
    except RuntimeError as missing:
        pytest.skip(str(missing))
    base, port = tmp_path_factory.mktemp("name-search"), free_port()
    url, log = f"http://127.0.0.1:{port}", base / "service.log"
    env = {"NVNM_RPC": tempo.rpc_url, "DB_PATH": str(base / "names.db"), "BIND": url[7:], "POLL_SECONDS": "0.2"}
    with open(log, "w") as out:
        proc = subprocess.Popen(
            [exe, "serve"], cwd=base, env=os.environ | env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
        )
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"nvnmchain-anchoring did not come up (exit {proc.returncode}):\n{log.read_text()[-3000:]}")
            try:
                if requests.get(f"{url}/health", timeout=2).ok:
                    break
            except requests.ConnectionError:
                pass
            time.sleep(0.2)
        yield url
    finally:
        terminate_process_group(proc)


def search(base: str, name: str, mode: str = "EXACT") -> list[dict]:
    params = {"name": name, "mode": f"REGISTRY_NAME_MATCH_MODE_{mode}"}
    response = requests.get(f"{base}{SEARCH_PATH}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()["registries"]


def test_the_seeded_registries(name_search):
    # SeedFixture.t.sol's: us-ca1 (1 and 3) and us-ca9 (2), by Alice and Bob.
    assert [r["id"] for r in search(name_search, "US-CA", "PREFIX")][:3] == ["1", "2", "3"]
    assert [r["id"] for r in search(name_search, "ca9", "SUFFIX")] == ["2"]
    assert search(name_search, "us-ca9") == [
        {
            "id": "2",
            "name": "us-ca9",
            "description": "Ninth Circuit",
            "creator": bech32("0x0000000000000000000000000000000000000B0b"),
            "created_at": "2025-09-09 00:00:00 +0000 UTC",
            "metadata": "{}",
        }
    ]


async def test_a_new_registry_is_found_once_polled(w3, chain_id, funded_account, name_search):
    tag = secrets.token_hex(4)
    name = f"Live Fund {tag}"
    data = ANCHORING.fns.addRegistry(name, "", "{}").data
    receipt = await send_call(w3, chain_id, funded_account, ANCHORING_ADDRESS, data)
    [(_, registry_id, _)] = emitted(receipt, "AddRegistry")

    deadline = time.monotonic() + 30
    while not (found := search(name_search, tag.upper(), "CONTAINS")):
        assert time.monotonic() < deadline, "not indexed after 30 s"
        await asyncio.sleep(0.2)
    block = await w3.eth.get_block(receipt["blockNumber"])
    assert found == [
        {
            "id": str(registry_id),
            "name": name,
            "description": "",
            "creator": bech32(funded_account.address),
            "created_at": time.strftime(GO_TIME, time.gmtime(block["timestamp"])),
            "metadata": "{}",
        }
    ]
    on_chain = [r.id for r in await registries_by_name(w3, name)]
    assert [int(r["id"]) for r in search(name_search, name)] == on_chain, "exact agrees with the contract"
