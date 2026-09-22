"""nvnmchain-anchoring translating the node's search onto the module's REST route, which is the
only part the node does not serve itself."""

import os
import subprocess
import time

import pytest
import requests

from .anchoring import anchoring_node, bech32
from .network import _resolve_bin, free_port, terminate_process_group

pytestmark = [pytest.mark.tempo, pytest.mark.anchoring]

SEARCH_PATH = "/NVNM-Chain/nvnmchain/anchoring/v1/registries/search"


@pytest.fixture(scope="module")
def tempo(tmp_path_factory):
    """This module's node, in place of the session's, running the index the service translates."""
    # Its own name, so pytest's `anchoringcurrent` link stays test_anchoring.py's node.
    with anchoring_node(tmp_path_factory.mktemp("anchoring-service"), extra_args=["--anchoring.name-index"]) as node:
        yield node


@pytest.fixture(scope="module")
def name_search(tempo, tmp_path_factory):
    """The service over that node, which it asks for the index once before it listens."""
    try:
        exe = _resolve_bin("nvnmchain-anchoring", "ANCHORING_BIN")
    except RuntimeError as missing:
        pytest.skip(str(missing))
    base, bind = tmp_path_factory.mktemp("name-search"), f"127.0.0.1:{free_port()}"
    url, log = f"http://{bind}", base / "service.log"
    with open(log, "w") as out:
        proc = subprocess.Popen(
            [exe, "serve"],
            cwd=base,
            env=os.environ | {"NVNM_RPC": tempo.rpc_url, "BIND": bind},
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if requests.get(f"{url}/health", timeout=2).ok:
                    break
            except requests.ConnectionError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"nvnmchain-anchoring did not come up (exit {proc.returncode}):\n{log.read_text()[-3000:]}")
            time.sleep(0.2)
        yield url
    finally:
        terminate_process_group(proc)


def search(base: str, name: str, mode: str = "EXACT") -> list[dict]:
    params = {"name": name, "mode": f"REGISTRY_NAME_MATCH_MODE_{mode}"}
    response = requests.get(f"{base}{SEARCH_PATH}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()["registries"]


def test_a_string_id_and_snake_case_fields(name_search):
    # SeedFixture.t.sol's us-ca9, by Bob. The node answers a number and camelCase.
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


def test_the_enum_spelling_on_the_query_string(name_search):
    # One registry through every mode: the name reaches the right matcher, nothing more.
    for name, mode in (("us-ca9", "EXACT"), ("us-ca9", "PREFIX"), ("ca9", "SUFFIX"), ("s-ca9", "CONTAINS")):
        assert [r["id"] for r in search(name_search, name, mode)] == ["2"], mode


def test_the_health_route_renames_what_the_node_reports(name_search):
    # The node answers `lastId` and `registryCount`; whether it is caught up is its own suite's.
    health = requests.get(f"{name_search}/health", timeout=10).json()
    assert set(health) == {"last_id", "registry_count", "error"}
