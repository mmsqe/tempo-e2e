"""The node's own registry name index, on a node started with ``--anchoring.name-index``."""

import asyncio
import secrets
import time

import pytest

from .abi import ANCHORING, ANCHORING_ADDRESS
from .anchoring import (
    GO_TIME,
    Page,
    anchoring_node,
    bech32,
    emitted,
    new_registry,
    registries,
    registries_by_name,
)
from .utils import call_revert, send_call

pytestmark = [pytest.mark.tempo, pytest.mark.anchoring]


@pytest.fixture(scope="module")
def tempo(tmp_path_factory):
    """This module's node, in place of the session's: only it runs the index."""
    # Its own name, so pytest's `anchoringcurrent` link stays test_anchoring.py's node.
    with anchoring_node(tmp_path_factory.mktemp("anchoring-name-index"), extra_args=["--anchoring.name-index"]) as node:
        yield node


async def search(w3, name: str, mode: str = "exact", **page) -> list[dict]:
    """``anchoring_searchRegistriesByName``, which only a node running the index answers."""
    response = await w3.provider.make_request(
        "anchoring_searchRegistriesByName", [{"name": name, "mode": mode, **page}]
    )
    assert "error" not in response, response
    return response["result"]["registries"]


async def status(w3) -> dict:
    return (await w3.provider.make_request("anchoring_nameIndexStatus", []))["result"]


def ids(rows: list[dict]) -> set[int]:
    return {r["id"] for r in rows}


async def indexed(w3, registry_id: int, timeout: float = 30) -> None:
    """Wait until the index has read as far as ``registry_id``."""
    deadline = time.monotonic() + timeout
    while (await status(w3))["lastId"] < registry_id:
        assert time.monotonic() < deadline, f"registry {registry_id} not indexed after {timeout} s"
        await asyncio.sleep(0.2)


class TestWhatIsIndexed:
    """Both ways in. The seed corpus arrives in the dump and emits no ``AddRegistry`` log, so
    finding it proves the index reads state rather than blocks; a later registry proves the rest."""

    async def test_the_seeded_registries(self, w3):
        """SeedFixture.t.sol's: us-ca1 (1 and 3) and us-ca9 (2), by Alice and Bob."""
        assert [r["id"] for r in await search(w3, "US-CA", "prefix")][:3] == [1, 2, 3]
        assert [r["id"] for r in await search(w3, "ca9", "suffix")] == [2]
        assert [r["id"] for r in await search(w3, "S-CA9", "contains")] == [2]
        assert await search(w3, "us-ca9") == [
            {
                "id": 2,
                "name": "us-ca9",
                "description": "Ninth Circuit",
                "creator": bech32("0x0000000000000000000000000000000000000B0b"),
                "createdAt": "2025-09-09 00:00:00 +0000 UTC",
                "metadata": "{}",
            }
        ]

    async def test_it_holds_what_the_contract_holds(self, w3):
        reached = await status(w3)
        assert reached["lastId"] == reached["registryCount"] != 0
        assert reached["blockNumber"] >= 1, "the index follows blocks, not only the backfill"

    async def test_a_new_registry_arrives_with_its_block(self, w3, chain_id, funded_account):
        tag = secrets.token_hex(4)
        name = f"Live Fund {tag}"
        receipt = await send_call(
            w3, chain_id, funded_account, ANCHORING_ADDRESS, ANCHORING.fns.addRegistry(name, "", "{}").data
        )
        [(_, registry_id, _)] = emitted(receipt, "AddRegistry")
        await indexed(w3, registry_id)

        block = await w3.eth.get_block(receipt["blockNumber"])
        assert await search(w3, tag.upper(), "contains") == [
            {
                "id": registry_id,
                "name": name,
                "description": "",
                "creator": bech32(funded_account.address),
                "createdAt": time.strftime(GO_TIME, time.gmtime(block["timestamp"])),
                "metadata": "{}",
            }
        ]
        assert [r["id"] for r in await search(w3, name)] == [r.id for r in await registries_by_name(w3, name)]


class TestQueries:
    """Every mode, over one set of names."""

    async def test_every_mode_against_one_set_of_names(self, w3, chain_id, funded_account):
        """Names are not unique, so an answer is a set; every mode folds case."""
        tag = secrets.token_hex(3)
        base = f"nx-{tag}-index"
        mixed = await new_registry(w3, chain_id, funded_account, base.upper())
        lower = await new_registry(w3, chain_id, funded_account, base)
        tail = await new_registry(w3, chain_id, funded_account, f"{base}-tail")
        both, all_three = {mixed, lower}, {mixed, lower, tail}
        await indexed(w3, tail)

        for name, mode, expected in (
            # The numbers `RegistryNameMatchMode` used, so a caller keeps its spelling: 0 is
            # UNSPECIFIED, which is exact, and 1 says so.
            (base, "0", both),
            (base, "1", both),
            (base, "exact", both),
            (base.upper(), "exact", both),
            (f"{base}-tail", "exact", {tail}),
            # Exact stays exact: no partial, no trimming.
            (f"{base}-nope", "exact", set()),
            (base[:-1], "exact", set()),
            (f"{base} ", "exact", set()),
            # Prefix, suffix and contains stay anchored.
            (f"nx-{tag}", "prefix", all_three),
            (f"NX-{tag}".upper(), "prefix", all_three),
            (f"x-{tag}", "prefix", set()),
            ("-tail", "suffix", {tail}),
            (f"{tag}-index", "suffix", both),
            ("-tai", "suffix", set()),
            (f"{tag}-index", "contains", all_three),
            (f"{tag}-INDEX".upper(), "contains", all_three),
        ):
            assert ids(await search(w3, name, mode)) == expected, (name, mode)

        assert {r.id for r in await registries_by_name(w3, base)} == both, "exact agrees with the contract"
        assert all_three <= {r.id for r in await registries(w3, page=Page(limit=200))}

    async def test_a_metacharacter_matches_itself(self, w3, chain_id, funded_account):
        """An index served by LIKE has to escape `%` and `_` or a query turns into a wildcard over
        every registry; this one seeks bytes, so there is nothing to escape."""
        tag = secrets.token_hex(3)
        wild = await new_registry(w3, chain_id, funded_account, f"nx-{tag}-100%-safe_ty")
        plain = await new_registry(w3, chain_id, funded_account, f"nx-{tag}-100-safety")
        await indexed(w3, plain)

        assert ids(await search(w3, f"nx-{tag}-100%", "prefix")) == {wild}
        assert ids(await search(w3, "safe_ty", "suffix")) == {wild}
        assert ids(await search(w3, "safety", "suffix")) == {plain}
        # A wildcard reading of either would match the other name too.
        assert ids(await search(w3, f"nx-{tag}-100%-safety", "exact")) == set()
        assert ids(await search(w3, f"{tag}-100%-safe", "contains")) == {wild}

    async def test_a_contains_shorter_than_a_trigram_is_answered(self, w3, chain_id, funded_account):
        """A trigram index refuses a contains under three characters. This one scans."""
        tag = secrets.token_hex(3)
        registry = await new_registry(w3, chain_id, funded_account, f"nx-{tag}-zz")
        await indexed(w3, registry)

        for short in ("z", "zz"):
            assert registry in ids(await search(w3, short, "contains", limit=200)), short

    async def test_a_page_is_cut_by_offset_and_limit(self, w3):
        """The seed fixture's three us-ca names, which is enough to page."""
        whole = [r["id"] for r in await search(w3, "us-ca", "prefix")]
        assert len(whole) >= 3, whole
        assert [r["id"] for r in await search(w3, "us-ca", "prefix", limit=2)] == whole[:2]
        assert [r["id"] for r in await search(w3, "us-ca", "prefix", offset=1, limit=2)] == whole[1:3]
        assert await search(w3, "us-ca", "prefix", offset=len(whole)) == []


class TestRefusals:
    """What each side will not answer."""

    async def test_the_index_refuses_a_query(self, w3):
        for params in ({"name": ""}, {"name": "us", "mode": "regex"}):
            response = await w3.provider.make_request("anchoring_searchRegistriesByName", [params])
            assert response["error"]["code"] == -32602, response

    async def test_the_contract_answers_only_exact(self, w3):
        """Nothing in block execution can read the index, so nodes disagreeing about it — one
        indexing, one not — cannot make them disagree about a block."""
        for mode in (2, 3, 4):
            err = await call_revert(w3, ANCHORING_ADDRESS, ANCHORING.fns.registriesByName("us-ca1", mode, Page()).data)
            assert "only exact match is on chain" in err, (mode, err)

        err = await call_revert(w3, ANCHORING_ADDRESS, ANCHORING.fns.registriesByName("us-ca1", 9, Page()).data)
        assert "invalid matchMode" in err, err
