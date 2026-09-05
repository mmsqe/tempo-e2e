"""nvnmchain-anchoring's HTTP service, against a real chain and a real index.

The projections it serves are the ones this suite used to re-implement in Python and query
directly. These tests run *its* implementation instead, against the same oracle that SQL was
checked by: what ``/roles`` serves is what ``hasRole`` answers. Nothing is lost by not writing
the query twice, and these are now the only tests over the projections.

Needs ``--tidx`` and the service binary; ``binary`` says what a missing one does. Two tests
migrate a real export where ``NVNM_EXPORT_DIR`` says it is; ``real_export`` says what they do
without it.
"""

import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import pytest
from eth_utils import keccak
from hexbytes import HexBytes

from .abi import MMR_VERIFIER
from .abi import REGISTRY as REG
from .anchoring import batches_of, hash_leaf, hash_merge, leaves_of
from .network import _resolve_bin, free_port
from .registry import ADMIN, EDITOR, deployed_address
from .utils import DEPLOY_GAS, STATE_WRITE_GAS, funded, new_account, send_call, send_calls

pytestmark = pytest.mark.tempo


def binary() -> str:
    """The service under test, or a skip -- unless skipping would be a lie.

    Resolved like every other binary this suite runs: ``$ANCHORING_BIN``, else on PATH, never a
    relative checkout -- where someone happens to have cloned a sibling repo is not something to
    encode in a test. An installed copy older than the tree under test fails loudly, because the
    contracts moved under it. A missing one leaves the projections unchecked entirely, which in
    CI would read as a passing suite, so ``ANCHORING_BIN_REQUIRED`` makes that a failure.
    """
    try:
        return _resolve_bin("nvnmchain-anchoring", "ANCHORING_BIN")
    except RuntimeError as missing:
        if os.environ.get("ANCHORING_BIN_REQUIRED"):
            pytest.fail(str(missing))
        pytest.skip(str(missing))


def staged_export(directory, name: str, rows) -> str:
    """A mainnet-full-export subset of one registry, as the migration reads it."""
    return staged_exports(directory, {name: rows})


def staged_exports(directory, registries: dict) -> str:
    """An export of several registries, as the migration reads it: a tranche file per registry,
    the manifest that has to match each byte for byte, and the listing."""
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for name, rows in registries.items():
        lines = [
            json.dumps(
                {
                    "registry": name,
                    "uri": uri,
                    "checksum": checksum,
                    "checksumAlgo": "sha256",
                    "metadata": "{}",
                    "status": status,
                }
            )
            for checksum, uri, status in rows
        ]
        content = ("\n".join(lines) + "\n").encode()
        archive = gzip.compress(content, mtime=0)
        (directory / f"{name}.jsonl.gz").write_bytes(archive)
        files.append(
            {
                "registry": name,
                "records": len(lines),
                "file": f"{name}.jsonl.gz",
                "tranche": 1,
                "sha256_gz": hashlib.sha256(archive).hexdigest(),
                "sha256_uncompressed": hashlib.sha256(content).hexdigest(),
            }
        )
    (directory / "registries.json").write_text(
        json.dumps([{"name": name, "description": "the docs", "metadata": "{}"} for name in registries])
    )
    totals = {"registries": len(files), "records": sum(f["records"] for f in files)}
    (directory / "manifest.json").write_text(json.dumps({"totals": totals, "files": files}))
    return str(directory)


def _answer(request: urllib.request.Request, *, expect: int) -> dict:
    """The JSON a request came back with, asserting the status. A failure is a JSON body
    with an ``error`` key rather than an empty result, which is the service's contract."""
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 - fixed localhost URL
            body, status = json.load(resp), resp.status
    except urllib.error.HTTPError as e:
        body, status = json.load(e), e.code
    assert status == expect, f"{request.full_url} -> {status}: {body}"
    return body


def get(url: str, *, expect: int = 200) -> dict:
    return _answer(urllib.request.Request(url), expect=expect)


def post(url: str, body, *, expect: int = 200) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    return _answer(request, expect=expect)


def records_of(base: str, registry) -> list[dict]:
    return get(f"{base}/registries/{registry.address}/records")["records"]


def roles_of(base: str, registry) -> list[dict]:
    return get(f"{base}/registries/{registry.address}/roles")["roles"]


def await_health(proc, base: str, status: int = 200) -> None:
    """Blocks until the service answers ``/health`` with ``status``, or says why it never will.

    The status is a parameter because a service pointed at an index it cannot reach is up and
    serving -- it answers 502, and refusing to wait for that would leave the failure paths
    untestable.
    """
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"service exited {proc.returncode}: {proc.stdout.read()}")
        try:
            get(f"{base}/health", expect=status)
            return
        except (urllib.error.URLError, ConnectionError, AssertionError):
            time.sleep(0.2)
    proc.terminate()
    pytest.fail(f"service did not answer /health with {status} in 30s")


def settings(tempo, tidx, factory, **env) -> dict:
    """The environment the binary reads its configuration from -- serving or not.

    An ``env`` value of ``None`` unsets the variable rather than emptying it, which is a
    different configuration: the binary validates what it is given and refuses to start on a
    malformed address, where an absent one is a legitimate audit-only setup.
    """
    return {
        k: v
        for k, v in {
            **os.environ,
            "CHAIN_ID": str(tempo.chain_id),
            "TIDX_URL": f"http://127.0.0.1:{tidx.api_port}",
            "NVNM_RPC": tempo.rpc_url,
            "FACTORY_ADDRESS": factory.address,
            **env,
        }.items()
        if v is not None
    }


def cli(tempo, tidx, factory, *args, expect: int = 0, raw: bool = False):
    """What one command printed: its JSON if it printed one, else the message it printed
    instead -- a refusal, a missing argument. ``raw`` for a plan, which is JSON per line."""
    proc = subprocess.run(  # noqa: S603 - a binary this suite resolved itself
        [binary(), *args],
        env=settings(tempo, tidx, factory),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == expect, f"{args} -> {proc.returncode}: {proc.stderr}"
    if raw:
        return proc.stdout
    printed = proc.stdout.strip()
    return json.loads(printed) if printed else proc.stderr


def planned(tempo, tidx, factory, export: str, *flags) -> tuple[list[dict], Path]:
    """A staged export's migration plan: its steps, and the file ``reconcile`` reads them
    back from. No ``--threshold`` roots everything, which is the planner's own default."""
    printed = cli(
        tempo,
        tidx,
        factory,
        "migrate",
        f"--registries={export}/registries.json",
        f"--manifest={export}/manifest.json",
        f"--export={export}",
        *flags,
        raw=True,
    )
    plan = Path(export) / "plan.jsonl"
    plan.write_text(printed)
    return [json.loads(line) for line in printed.splitlines()], plan


class Export(NamedTuple):
    """A real export: its directory, its manifest files smallest first, its listing by name."""

    dir: Path
    files: list[dict]
    registries: dict[str, dict]


def real_export() -> Export:
    """Where ``$NVNM_EXPORT_DIR`` says it is; a skip otherwise. The listing and manifest are
    enough to plan by sha256 root; replaying needs the tranche files beside them."""
    staged = os.environ.get("NVNM_EXPORT_DIR")
    if not staged:
        pytest.skip("NVNM_EXPORT_DIR is unset: no real export to migrate")
    directory = Path(staged)
    files = sorted(json.loads((directory / "manifest.json").read_text())["files"], key=lambda f: f["records"])
    registries = {r["name"]: r for r in json.loads((directory / "registries.json").read_text())}
    return Export(directory, files, registries)


def smallest_and_largest(files: list[dict]) -> list[dict]:
    """The three smallest registries and the largest; under four, all of them, so none is
    named twice -- a duplicate is a listing the planner refuses."""
    return files if len(files) <= 4 else files[:3] + files[-1:]


def subset_of(export: Export, directory: Path, chosen: list[dict]) -> str:
    """The export cut down to the manifest files ``chosen``, totals to match -- the planner
    refuses a listing and manifest that disagree -- and any staged tranche linked in."""
    directory.mkdir(parents=True, exist_ok=True)
    listing = [export.registries[f["registry"]] for f in chosen]
    (directory / "registries.json").write_text(json.dumps(listing))
    totals = {"registries": len(chosen), "records": sum(f["records"] for f in chosen)}
    (directory / "manifest.json").write_text(json.dumps({"totals": totals, "files": chosen}))
    for f in chosen:
        if (export.dir / f["file"]).exists():
            link = directory / f["file"]
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(export.dir / f["file"])
    return str(directory)


def mmr_siblings(commitments: list[bytes], index: int) -> list[bytes]:
    """The proof of leaf ``index`` in the MMR over ``commitments``: its siblings up to its peak,
    lowest first, hashed as the precompile hashes, with the peaks aligned to leaf positions."""
    n, start = len(commitments), 0
    for h in range(63, -1, -1):
        if n >> h & 1:
            if index < start + (1 << h):
                break
            start += 1 << h
    nodes, at, siblings = [hash_leaf(c) for c in commitments[start : start + (1 << h)]], index - start, []
    while len(nodes) > 1:
        siblings.append(nodes[at ^ 1])
        nodes, at = [hash_merge(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)], at // 2
    return siblings


class Migration:
    """A plan being sent, and the addresses its deploys hand back.

    Steps go in plan order because they depend on it: every step under a registry names
    it by the name its deploy carried, and the address exists once that deploy has landed.
    """

    def __init__(self, w3, chain_id, factory, tidx, sender):
        self.w3, self.chain_id, self.factory, self.tidx, self.sender = w3, chain_id, factory, tidx, sender
        self.deployed: dict[str, str] = {}

    async def send(self, steps):
        """One call per transaction, then waits for the index to cover them. A step
        ``reconcile`` hands back names its target; one straight from the plan is routed
        by what this run has deployed so far."""
        for step in steps:
            to = step.get("to") or (
                self.factory.address if step["kind"] == "deploy" else self.deployed[step["registry"]]
            )
            receipt = await send_calls(
                self.w3,
                chain_id=self.chain_id,
                private_key=self.sender.key.hex(),
                calls=[{"to": to, "data": bytes.fromhex(step["data"][2:])}],
                gas_limit=DEPLOY_GAS if step["kind"] == "deploy" else STATE_WRITE_GAS,
            )
            assert receipt["status"] == 1, step
            if step["kind"] == "deploy":
                self.deployed[step["registry"]] = deployed_address(receipt, self.factory.address)
        self.tidx.bounded(receipt)
        return receipt  # the last step's, which is the one a caller has anything to read


@contextmanager
def running(tempo, tidx, factory, *, healthy: int = 200, **env):
    """The service, from spawn to teardown, answering on the base URL yielded."""
    port = free_port()
    proc = subprocess.Popen(
        [binary(), "serve"],
        env=settings(tempo, tidx, factory, BIND=f"127.0.0.1:{port}", **env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        await_health(proc, base, healthy)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def service(tempo, tidx, factory):
    """Per test rather than per module: the factory address is startup config, and every
    test wants its own factory. Starting it costs a process spawn."""
    with running(tempo, tidx, factory) as base:
        yield base


@pytest.fixture
def paged_service(tempo, tidx, factory):
    """The same, fetching two rows per round trip.

    Every projection here is far under tidx's 10,000-row cap, so at the real page size the
    loop would never take a second lap and the cursor would go unexercised.
    """
    with running(tempo, tidx, factory, PAGE_SIZE="2") as base:
        yield base


class TestService:
    async def test_health_reports_how_far_the_index_reaches(self, w3, service, tidx):
        health = get(f"{service}/health")
        height = await w3.eth.block_number  # read after, so the chain has only grown since

        assert health["tip_num"] >= 1, health
        assert health["tip_num"] <= height, "the index cannot have reached past the chain"
        # `lag` is what is left to ingest below the head the index has *seen*, so a caught-up
        # one reports 0 rather than the distance to the chain's own head.
        assert 0 <= health["lag"] and health["tip_num"] + health["lag"] <= height, health
        assert isinstance(health["reaches_first_block"], bool), health

    async def test_every_answer_says_the_block_it_was_read_at(self, factory, registry, service, tidx):
        """Every projection is bounded at a block, so two calls either side of one legitimately
        differ -- ``at_block`` is what tells that from a projection that changed. For
        ``/records`` it is also the bound the numbering and the heads share.
        """
        wrote = tidx.bounded(await registry.add_record(registry.creator, "abc"))

        registries = get(f"{service}/registries")
        records = get(f"{service}/registries/{registry.address}/records")
        roles = get(f"{service}/registries/{registry.address}/roles")
        tip = get(f"{service}/health")["tip_num"]  # last, because the tip only grows

        # Each answer names what it was read from, so a reply cannot be attributed to the
        # wrong factory or registry.
        assert registries["factory"].lower() == factory.address.lower()
        assert records["registry"].lower() == roles["registry"].lower() == registry.address.lower()
        for body in (registries, records, roles):
            assert wrote <= body["at_block"] <= tip, body["at_block"]

    async def test_registries_are_listed_in_deployment_order(self, w3, factory, service, tidx):
        creator = await funded(w3)
        first = await factory.deploy(creator, "docs", "the first", "{}")
        second = await factory.deploy(creator, "photos")
        tidx.bounded(second.deployment)

        listed = get(f"{service}/registries")["registries"]
        by_address = {r["address"].lower(): r for r in listed}
        assert first.address.lower() in by_address, listed
        assert second.address.lower() in by_address, listed

        one, two = by_address[first.address.lower()], by_address[second.address.lower()]
        assert one["name"] == "docs"
        assert one["description"] == "the first"
        assert one["creator"].lower() == creator.address.lower()
        assert two["name"] == "photos"
        # Deployment order is the numbering -- the whole reason the contract
        # stopped keeping a counter.
        assert one["number"] < two["number"], (one, two)

    async def test_roles_agree_with_the_contract(self, registry, service, tidx):
        """What the service projects is what ``hasRole`` answers.

        The history is built so every simpler fold breaks: a re-grant after a revoke needs
        ordering rather than a set difference, one account holding both a record- and a
        registry-scoped role needs the checksum in the partition, and one grant stays revoked.
        """
        # Only the creator signs; the rest are subjects of grants and of `hasRole`, a call.
        creator = registry.creator  # its admin was announced at deployment, like any other grant
        both, regranted, revoked = new_account(), new_account(), new_account()

        await registry.add_record(creator, "abc")
        await registry.grant(creator, both, EDITOR)
        await registry.grant(creator, both, EDITOR, checksum="abc")
        await registry.grant(creator, regranted, EDITOR)
        await registry.revoke(creator, regranted, EDITOR)
        await registry.grant(creator, revoked, EDITOR)
        await registry.revoke(creator, revoked, EDITOR)
        tidx.bounded(await registry.grant(creator, regranted, EDITOR))  # the head for its key

        served = {(HexBytes(r["scope"]), r["account"].lower(), r["role"]) for r in roles_of(service, registry)}
        expected = [
            (creator, ADMIN, ""),
            (both, EDITOR, ""),
            (both, EDITOR, "abc"),  # same account and role, a different partition
            (regranted, EDITOR, ""),  # granted, revoked, granted again
        ]
        assert served == {
            (HexBytes(keccak(text=scope)), account.address.lower(), role.rstrip(b"\x00").decode())
            for account, role, scope in expected
        }, served

        # ...and against the contract itself, not only against our expectation.
        for account, role, scope in expected:
            assert await registry.has_role(account, role, checksum=scope)
        assert not await registry.has_role(revoked, EDITOR), "one grant stays revoked"

    async def test_a_second_registry_does_not_reach_the_first(self, w3, factory, service, tidx):
        """Why no query needs a registry id: the address in the path is the partition.

        Two registries, the same account granted in one only. A projection that leaked across
        addresses would answer with both — which is what the old registryId narrowing was for.
        """
        creator, editor = await funded(w3), new_account()
        a = await factory.deploy(creator, "a")
        b = await factory.deploy(creator, "b")
        await a.grant(creator, editor, EDITOR)
        tidx.bounded(await b.grant(creator, editor, ADMIN))

        def held(registry):
            return {(r["account"].lower(), r["role"]) for r in roles_of(service, registry)}

        assert (editor.address.lower(), "editor") in held(a)
        assert (editor.address.lower(), "admin") in held(b)
        assert (editor.address.lower(), "admin") not in held(a)
        assert (editor.address.lower(), "editor") not in held(b)

    async def test_each_registry_numbers_its_own_records(self, w3, factory, service, tidx):
        """What the contract's per-registry counter used to guarantee, now the address
        filter's job: the same checksum is a different record number in each registry."""
        creator = await funded(w3)
        a = await factory.deploy(creator, "a")
        b = await factory.deploy(creator, "b")
        await b.add_record(creator, "earlier")
        await a.add_record(creator, "shared")
        tidx.bounded(await b.add_record(creator, "shared"))

        def numbering(registry):
            return {r["checksum"]: r["number"] for r in records_of(service, registry)}

        assert numbering(a) == {"shared": 1}
        assert numbering(b) == {"earlier": 1, "shared": 2}

    async def test_a_new_version_does_not_renumber_its_record(self, registry, service, tidx):
        """A record is numbered where its stream started -- what the module's `recordId` did by
        assigning it on the first write and carrying it forward.

        Written in the order that catches numbering by newest version instead: the older
        record is the one that gets a second one.
        """
        creator = registry.creator
        await registry.add_record(creator, "first")
        await registry.add_record(creator, "second")
        tidx.bounded(await registry.add_record(creator, "first", uri="ipfs://v2"))

        numbering = {r["checksum"]: (r["number"], r["version"]) for r in records_of(service, registry)}
        assert numbering == {"first": (1, 2), "second": (2, 1)}, numbering

    async def test_records_come_back_decoded_at_their_newest_version(self, registry, service, tidx):
        creator = registry.creator
        await registry.add_record(creator, "abc", uri="ipfs://v1")
        await registry.add_record(creator, "abc", uri="ipfs://v2")
        tidx.bounded(await registry.set_status(creator, "abc", 2, "approved"))

        records = records_of(service, registry)
        assert len(records) == 1, records
        record = records[0]
        # The envelope, decoded -- none of this is readable from the log without it.
        assert record["checksum"] == "abc"
        assert record["checksum_algo"] == "sha256"
        assert HexBytes(record["checksum_hash"]) == HexBytes(keccak(text="abc"))
        assert record["version"] == 2, "the head is the newest version, not the first"
        assert record["uri"] == "ipfs://v2"
        assert record["status"] == "approved", "anchored against version 2, so it applies"
        assert record["number"] == 1, "the id the contract stopped assigning"
        # Attribution and classification are envelope-only too: the precompile's caller is
        # the registry contract, so nothing in the log's topics says who wrote a version.
        assert record["author"] == creator.address, "checksummed, like every address served"
        assert record["category"] == 0, "Unspecified: the writer claimed no category"
        assert record["data_pointer"] == ""

    async def test_the_fields_only_the_decoder_can_read_come_back(self, w3, factory, service, tidx):
        """``metadata`` is the field this crate exists for: tidx hands a dynamic ``bytes``
        argument back as its ABI offset word, so a column taken off a decoded event table
        would read ``0x…40`` for every record ever anchored.

        The timestamp is in the envelope too, and it is the block's -- the contract does not
        accept one, where the module took one and overwrote it. A registry's own metadata
        comes from the deployment log instead, never anchored.
        """
        creator = await funded(w3)
        registry = await factory.deploy(creator, "docs", "the docs", '{"src":"e2e"}')
        metadata = '{"document":"Record 1","figi":"","individualId":""}'
        receipt = await registry.add_record(creator, "abc", uri="ipfs://abc", metadata=metadata)
        tidx.bounded(receipt)

        (record,) = records_of(service, registry)
        assert record["metadata"] == metadata
        assert record["timestamp"] == (await w3.eth.get_block(receipt["blockNumber"]))["timestamp"]

        listed = {r["address"].lower(): r for r in get(f"{service}/registries")["registries"]}
        assert listed[registry.address.lower()]["metadata"] == '{"src":"e2e"}'

    async def test_an_oversized_field_comes_back_whole(self, registry, service, tidx):
        """The module capped `checksum_algo` at 128 bytes and refused the rest; nothing caps it
        now, so what was a validation rule is a decoding one. 129 bytes is the value that used
        to fail, and the kilobyte of metadata beside it is the field tidx cannot decode at all.
        """
        algo, metadata = "a" * 129, json.dumps({"pad": "m" * 1024})

        tidx.bounded(await registry.add_record(registry.creator, "abc", uri="ipfs://big", algo=algo, metadata=metadata))

        (record,) = records_of(service, registry)
        assert record["checksum_algo"] == algo
        assert record["metadata"] == metadata

    async def test_a_status_against_an_older_version_is_not_the_current_one(self, registry, service, tidx):
        """Statuses are keyed per version, and the newest version carries none of its own."""
        creator = registry.creator
        await registry.add_record(creator, "abc", uri="ipfs://v1")
        await registry.set_status(creator, "abc", 1, "approved")
        tidx.bounded(await registry.add_record(creator, "abc", uri="ipfs://v2"))

        (record,) = records_of(service, registry)
        assert record["version"] == 2
        assert record["status"] is None, "version 1's status is not version 2's"

    async def test_an_unreachable_index_is_a_gateway_error(self, tempo, tidx, factory):
        """502 on every projection, each with an ``error`` in the body. An index that cannot
        be reached answering "nothing here" is the failure this crate exists to notice, and
        it is indistinguishable from a chain where nothing was ever anchored.
        """
        nowhere = f"http://127.0.0.1:{free_port()}"  # free, so nothing is listening on it

        with running(tempo, tidx, factory, healthy=502, TIDX_URL=nowhere) as base:
            for path in ("/health", "/registries", f"/registries/{factory.address}/roles"):
                assert "error" in get(f"{base}{path}", expect=502), path

    async def test_a_missing_factory_is_this_process_misconfigured(self, tempo, tidx, factory, registry):
        """500, not 502: an operator sent to look at tidx would find nothing wrong there.

        An unset factory is a legitimate configuration -- the audit needs none -- so only
        ``/registries`` has nothing to answer with, and it says so rather than serving the
        empty page that would look like a factory which has deployed nothing.
        """
        tidx.bounded(registry.deployment)

        with running(tempo, tidx, factory, FACTORY_ADDRESS=None) as base:
            assert "error" in get(f"{base}/registries", expect=500)
            assert len(roles_of(base, registry)) == 1, "what needs no factory still answers"

    async def test_registries_are_filtered_by_name(self, w3, factory, service, tidx):
        """The module's `registriesByName`, over a listing whose names live in the deployment
        log rather than in state.

        Two registries share a name with a third interleaved, so the filter has to be
        multi-valued rather than resolve to one -- a name is not an identifier here, the
        address is. Byte-exact for the same reason: folding case would answer about a registry
        the caller did not name.
        """
        creator = await funded(w3)
        shared = "name-filter"
        first = await factory.deploy(creator, shared)
        other = await factory.deploy(creator, f"{shared}-other")
        second = await factory.deploy(creator, shared)
        tidx.bounded(second.deployment)

        def listed(**params) -> set:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            return {r["address"].lower() for r in get(f"{service}/registries?{query}")["registries"]}

        both = {first.address.lower(), second.address.lower()}
        all_three = both | {other.address.lower()}
        for filters, expected in (
            ({"name": shared}, both),
            ({"name": f"{shared}-other"}, {other.address.lower()}),
            # An unknown name is an empty page rather than an error, and no mode folds case,
            # trims, or settles for a partial match.
            ({"name": f"{shared}-nope"}, set()),
            ({"name": shared.upper()}, set()),
            ({"name": f"{shared}%20"}, set()),
            ({"name": shared[:-1]}, set()),
            # Prefix, suffix and contains match substrings, and stay anchored.
            ({"name_prefix": shared}, all_three),
            ({"name_suffix": "-other"}, {other.address.lower()}),
            ({"name_contains": "me-fil"}, all_three),
            ({"name_prefix": "ame-filter"}, set()),
            ({"name_suffix": "-othe"}, set()),
            # Filters combine with AND, so a contradictory pair returns nothing rather than
            # one of them quietly winning.
            ({"name_prefix": shared, "name_suffix": "-other"}, {other.address.lower()}),
            ({"name": shared, "name_suffix": "-other"}, set()),
        ):
            assert listed(**filters) == expected, filters

        assert listed() == all_three, "no filter leaves the listing alone"
        # Numbered by deployment order, which the filter does not renumber.
        filtered = get(f"{service}/registries?name={shared}")["registries"]
        assert [r["number"] for r in filtered] == [1, 3], filtered

        # A parameter this service does not know is a 400: ignored, it would answer with
        # every registry the factory ever deployed and read as a filter that matched them all.
        assert "error" in get(f"{service}/registries?name_prefx={shared}", expect=400)

    async def test_the_command_line_answers_what_the_service_does(self, tempo, tidx, factory, registry, service):
        """The read half of `nvnmchaind query anchoring …`, for an operator with no service
        running: the same projections, from the same binary, straight out of the index.

        Compared without ``at_block``, which each call reads for itself -- the chain moves
        between them, and that field is the answer saying so.
        """
        tidx.bounded(await registry.add_record(registry.creator, "cli"))

        for argv, url in [
            (["registries"], f"{service}/registries"),
            (["registries", "--name=docs"], f"{service}/registries?name=docs"),
            (["records", registry.address], f"{service}/registries/{registry.address}/records"),
            (["roles", registry.address], f"{service}/registries/{registry.address}/roles"),
            (["record", registry.address, "cli"], f"{service}/registries/{registry.address}/records/cli"),
            (["checksum", "cli"], f"{service}/records/cli"),
        ]:
            printed, served = cli(tempo, tidx, factory, *argv), get(url)
            assert printed.pop("at_block") <= served.pop("at_block"), argv
            assert printed == served, argv

        # And it refuses what the service refuses, with an exit code for the same split:
        # 2 for a question that cannot be answered as asked.
        message = cli(tempo, tidx, factory, "records", "0x" + "ad" * 20, expect=2)
        assert "not a registry deployed by" in message, message

    async def test_a_migration_plan_lands_and_reconciles(self, w3, chain_id, tempo, tidx, factory, tmp_path):
        """The planner's calldata against the real contracts, and its reconciliation against
        the real projections -- its own tests pin the encoding against an independent
        encoder, which says nothing about whether the contracts take it.

        Sent in two halves, because that is the path an operator takes after anything goes
        wrong mid-run: ask the chain what is left, send that, and reconcile clean.
        """
        rows = [("abc", "ipfs://v1", ""), ("abc", "ipfs://v2", "approved"), ("def", "ipfs://d", "")]
        export = staged_export(tmp_path, "docs", rows)
        steps, plan = planned(tempo, tidx, factory, export, "--threshold=100")
        assert [s["kind"] for s in steps] == ["deploy", "record", "record", "status", "record"]

        migration = Migration(w3, chain_id, factory, tidx, await funded(w3))

        # A run that stops halfway resumes from what the chain holds, never from how far it
        # got. Steps still owed are not a failure -- exit 0 -- so a loop can run on the exit
        # code and the file alone.
        await migration.send(steps[:2])
        left = tmp_path / "left.jsonl"
        answer = cli(tempo, tidx, factory, "reconcile", f"--plan={plan}", f"--remaining={left}")
        assert answer == {"divergences": [], "remaining": 3}, answer
        # What it hands back names its target -- the registry the first half deployed -- so
        # a sender resuming needs no log of its own.
        owed = [json.loads(line) for line in left.read_text().splitlines()]
        assert {s["to"].lower() for s in owed} == {migration.deployed["docs"].lower()}, owed
        await migration.send(owed)

        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}

    async def test_a_registry_over_the_threshold_lands_as_one_merkle_root(
        self, w3, chain_id, tempo, tidx, factory, service, tmp_path
    ):
        """The planner's other half, and its default: above the threshold a whole export
        file lands as one record -- a merkle root over its lines, under an algo naming the
        tree, with the file it commits to in the metadata -- and none of its rows do."""
        rows = [("abc", "ipfs://v1", ""), ("abc", "ipfs://v2", "approved"), ("def", "ipfs://d", "")]
        export = staged_export(tmp_path, "docs", rows)
        steps, plan = planned(tempo, tidx, factory, export)  # no --threshold: everything roots

        assert [s["kind"] for s in steps] == ["deploy", "record"], "three rows, one record"

        migration = Migration(w3, chain_id, factory, tidx, await funded(w3))
        await migration.send(steps)

        (record,) = get(f"{service}/registries/{migration.deployed['docs']}/records")["records"]
        assert record["checksum_algo"] == "keccak256-merkle"
        assert record["checksum"] == steps[1]["checksum"], "the root the plan committed to"
        assert record["version"] == 1
        # The metadata is what makes the root redeemable: it names the file it stands for.
        legacy = json.loads(record["metadata"])["legacy"]
        assert legacy["registry"] == "docs"
        assert legacy["records"] == len(rows), "the rows it stands for, none of them anchored"
        assert legacy["file"] == "docs.jsonl.gz"
        assert record["uri"].endswith("/docs.jsonl.gz"), record["uri"]

        # And a rooted plan reconciles like any other.
        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}

    async def test_a_registry_over_the_threshold_can_load_as_leaves(
        self, w3, chain_id, tempo, tidx, factory, service, tmp_path
    ):
        """`--root=mmr`: the whole file as leaves of the registry's MMR in one call, the bulk
        anchor, where the merkle root above is one record. Nothing lands as a record; what the
        registry holds is the root, and that is what reconcile judges the step by."""
        rows = [("abc", "ipfs://v1", ""), ("abc", "ipfs://v2", "approved"), ("def", "ipfs://d", "")]
        export = staged_export(tmp_path, "docs", rows)
        steps, plan = planned(tempo, tidx, factory, export, "--root=mmr")
        assert [s["kind"] for s in steps] == ["deploy", "leaves"], "three rows, one call"

        migration = Migration(w3, chain_id, factory, tidx, await funded(w3))
        loaded = await migration.send(steps)
        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}

        registry = migration.deployed["docs"]
        # No leaf holds a key, so the precompile's log is the only word on where they landed.
        (batch,) = batches_of(loaded)
        assert (batch.namespace.lower(), batch.first, batch.count) == (registry.lower(), 0, len(rows)), (
            "into an empty MMR"
        )
        held = get(f"{service}/registries/{registry}/mmr")
        assert held["root"].lower() == steps[1]["checksum"].lower(), "the root the plan committed to"
        assert held["count"] == len(rows) and len(held["peaks"]) == 2, "three leaves: a pair and one"
        assert json.loads(held["metadata"])["legacy"]["mode"] == "leaves", "the plan's provenance"
        listing = get(f"{service}/registries/{registry}/records")
        assert listing["records"] == [] and listing["other"] == 0, "leaves are not records, and not leaves either"
        assert "error" in get(f"{service}/registries/{'0x' + 'ad' * 20}/mmr", expect=404)

        # The point of the structure: a record added later is one more leaf, with nothing to
        # carry from the batch, and it proves against the new root through the one verifier.
        later = keccak(b"a record added after the migration")
        count = held["count"]
        one = await send_call(w3, chain_id, migration.sender, registry, REG.fns.appendLeaf(later, b"").data)
        tidx.bounded(one)
        after = get(f"{service}/registries/{registry}/mmr")
        assert after["count"] == count + 1 and after["root"] != held["root"]
        (appended,) = leaves_of(one)
        assert (appended.index, appended.commitment) == (count, later), "on from the batch"
        assert HexBytes(appended.root) == HexBytes(after["root"]), "the root the event carries is served"
        assert get(f"{service}/registries/{registry}/records")["other"] == 1, "a bare leaf, counted"

        with gzip.open(Path(export) / "docs.jsonl.gz", "rt") as lines:
            leaves = [keccak(line.rstrip("\n").encode()) for line in lines] + [later]
        siblings = mmr_siblings(leaves, count)
        root, peaks = bytes(HexBytes(after["root"])), [bytes(HexBytes(p)) for p in after["peaks"]]
        proof = MMR_VERIFIER.fns.verify(root, later, count, siblings, peaks, after["count"])
        assert await proof.call(w3, to=factory.verifier) is True, "the later leaf proves"
        wrong = MMR_VERIFIER.fns.verify(root, keccak(b"another"), count, siblings, peaks, after["count"])
        assert await wrong.call(w3, to=factory.verifier) is False

        # The plan is superseded: it loaded into an empty MMR, and the MMR has moved on.
        answer = cli(tempo, tidx, factory, "reconcile", f"--plan={plan}", expect=1)
        assert answer["remaining"] == 0 and "MMR" in answer["divergences"][0]["detail"], answer

    async def test_a_plan_sent_batched_reconciles_clean(self, w3, chain_id, tempo, tidx, factory, tmp_path):
        """The plan sender as an operator runs it: several registries, deploys batched four to a
        transaction and the rest up to thirty-two, every receipt checked, reconciled clean.
        Three registries replay and two load as leaves, so both paths go through it.

        Four rather than three since the MMR left the contract: a registry deploys at about
        5.4M where it cost 7.4M, so one more fits under the sender's per-transaction budget.
        """
        one, two = [("a", "ipfs://a", "Active")], [("a", "ipfs://a", "Active"), ("b", "ipfs://b", "")]
        export = staged_exports(tmp_path / "export", {"r0": one, "r1": one, "r2": one, "r3": two, "r4": two})
        steps, plan = planned(tempo, tidx, factory, export, "--threshold=1", "--root=mmr")
        kinds = {k: sum(1 for s in steps if s["kind"] == k) for k in ("deploy", "record", "status", "leaves")}
        assert kinds == {"deploy": 5, "record": 3, "status": 3, "leaves": 2}, kinds

        sender = await funded(w3)
        argv = [sys.executable, "-m", "integration_tests.send_plan", f"--plan={plan}", f"--rpc={tempo.rpc_url}"]
        argv += [f"--chain-id={chain_id}", f"--factory={factory.address}"]
        env = {**os.environ, "PRIVATE_KEY": sender.key.hex()}
        dry = subprocess.run([*argv, "--dry-run"], capture_output=True, text=True, env=env, timeout=120)  # noqa: S603
        assert dry.returncode == 0, dry.stderr
        assert "tx 1: 4 deploys" in dry.stdout and "tx 2: 1 deploys" in dry.stdout, dry.stdout
        sent = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=600)  # noqa: S603
        assert sent.returncode == 0, sent.stderr
        assert "sent 13 steps" in sent.stdout, sent.stdout

        tidx.bounded((await factory.deploy(sender, "bound")).deployment)  # a later block: the index covers the sends
        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}

    async def test_a_step_sent_twice_is_reported_and_not_resent(self, w3, chain_id, tempo, tidx, factory, tmp_path):
        """The divergence that says a run was resumed by count rather than by chain state.

        `addRecord` appends a version every time it is called, so a re-sent step leaves the
        record one version past what the plan writes. That is reported and *not* put in
        `--remaining`: sending it again would only make it worse.
        """
        export = staged_export(tmp_path, "docs", [("abc", "ipfs://v1", "")])
        steps, plan = planned(tempo, tidx, factory, export, "--threshold=100")

        migration = Migration(w3, chain_id, factory, tidx, await funded(w3))
        await migration.send(steps)
        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}, (
            "clean once"
        )

        await migration.send([s for s in steps if s["kind"] == "record"])  # the same step again

        left = tmp_path / "left.jsonl"
        answer = cli(tempo, tidx, factory, "reconcile", f"--plan={plan}", f"--remaining={left}", expect=1)
        (divergence,) = answer["divergences"]
        assert "a step sent twice" in divergence["detail"], divergence
        assert answer["remaining"] == 0 and left.read_text() == "", "a version too many is not fixed by sending"

    async def test_the_real_export_lands_rooted_by_the_digests_it_carries(
        self, w3, chain_id, tempo, tidx, factory, service, tmp_path
    ):
        """A real listing and manifest against the real contracts: names, descriptions and
        metadata as calldata, each file rooted by the sha256 the manifest carries, so no
        tranche is needed. A subset, since a deploy is a transaction each: the three smallest
        registries and the largest."""
        export = real_export()
        chosen = smallest_and_largest(export.files)
        steps, plan = planned(tempo, tidx, factory, subset_of(export, tmp_path / "export", chosen), "--root=sha256")
        assert [s["kind"] for s in steps] == ["deploy", "record"] * len(chosen)

        migration = Migration(w3, chain_id, factory, tidx, await funded(w3))
        await migration.send(steps)
        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}

        listed = {r["name"]: r for r in get(f"{service}/registries")["registries"]}
        for f in chosen:
            name, source = f["registry"], export.registries[f["registry"]]
            assert listed[name]["description"] == source["description"]
            assert listed[name]["metadata"] == source["metadata"]
            (record,) = get(f"{service}/registries/{migration.deployed[name]}/records")["records"]
            assert record["checksum_algo"] == "sha256"
            assert record["checksum"].lower() == "0x" + f["sha256_uncompressed"]
            assert json.loads(record["metadata"])["legacy"]["records"] == f["records"]

    async def test_the_real_exports_rows_replay_and_its_files_root(
        self, w3, chain_id, tempo, tidx, factory, service, tmp_path
    ):
        """The planner's default over the tranche files: the three smallest registries replayed
        row by row, the largest verified against the manifest and rooted by merkle."""
        export = real_export()
        chosen = smallest_and_largest(export.files)
        if len(chosen) < 4:
            pytest.skip(f"{export.dir} has {len(chosen)} registries: too few to replay three and root one")
        replayed, rooted = chosen[:3], chosen[3]
        if not all((export.dir / f["file"]).exists() for f in chosen):
            pytest.skip(f"{export.dir} holds no tranche files: nothing to replay")
        threshold = max(f["records"] for f in replayed)
        if rooted["records"] <= threshold:
            pytest.skip(f"{export.dir}: no registry larger than the three replayed, nothing to root")
        steps, plan = planned(
            tempo, tidx, factory, subset_of(export, tmp_path / "export", chosen), f"--threshold={threshold}"
        )
        assert sum(s["kind"] == "deploy" for s in steps) == len(chosen)
        assert sum(s["kind"] == "record" for s in steps) == sum(f["records"] for f in replayed) + 1, "one root"

        migration = Migration(w3, chain_id, factory, tidx, await funded(w3))
        await migration.send(steps)
        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == {"divergences": [], "remaining": 0}

        for f in replayed:
            with gzip.open(export.dir / f["file"], "rt") as lines:
                rows = {
                    row["checksum"]: (row["uri"], row["checksumAlgo"], row["metadata"], row.get("status") or None)
                    for row in map(json.loads, lines)
                }
            records = get(f"{service}/registries/{migration.deployed[f['registry']]}/records")["records"]
            served = {r["checksum"]: (r["uri"], r["checksum_algo"], r["metadata"], r["status"]) for r in records}
            assert served == rows, f["registry"]

        (root,) = get(f"{service}/registries/{migration.deployed[rooted['registry']]}/records")["records"]
        committed = next(s for s in steps if s["registry"] == rooted["registry"] and s["kind"] == "record")
        assert root["checksum_algo"] == "keccak256-merkle"
        assert root["checksum"] == committed["checksum"], "the root the plan committed to"
        assert json.loads(root["metadata"])["legacy"]["records"] == rooted["records"]

    async def test_a_malformed_address_is_refused_rather_than_queried(self, service):
        # A filtered address would query a real-looking other one and answer
        # "nothing here" -- the quiet wrong answer this crate exists to notice.
        for bad in ["notanaddress", "0x1234", "0x" + "aa" * 21]:
            body = get(f"{service}/registries/{bad}/roles", expect=400)
            assert "error" in body, body

    async def test_a_records_versions_come_back_from_the_log(self, registry, service, tidx):
        """History is the half of a record the chain does not keep as such: every version is a
        leaf, and the listing shows the newest.

        Each version carries its own envelope, so the fields differ down the list -- and the
        status here is the one the listing cannot show, against a version no longer current.
        """
        creator = registry.creator
        await registry.add_record(creator, "abc", uri="ipfs://v1", metadata='{"v":1}')
        await registry.set_status(creator, "abc", 1, "approved")
        await registry.add_record(creator, "abc", uri="ipfs://v2", algo="sha512", metadata='{"v":2}')
        tidx.bounded(await registry.add_record(creator, "def", uri="ipfs://x"))

        answer = get(f"{service}/registries/{registry.address}/records/abc")
        versions = answer["versions"]

        assert [v["version"] for v in versions] == [1, 2]
        assert [v["uri"] for v in versions] == ["ipfs://v1", "ipfs://v2"]
        assert [v["checksum_algo"] for v in versions] == ["sha256", "sha512"]
        assert [v["metadata"] for v in versions] == ['{"v":1}', '{"v":2}']
        assert [v["status"] for v in versions] == ["approved", None], "each version's own"
        assert versions[0]["block_num"] <= versions[1]["block_num"]
        assert [v["leaf"] for v in versions] == [0, 2], "the leaf each version is; the status between them"

        # The listing still answers at the head, and the two agree on which that is -- and on
        # the number, which both pay a walk of the registry's ordering for.
        (record,) = [r for r in records_of(service, registry) if r["checksum"] == "abc"]
        assert (record["version"], record["uri"]) == (versions[-1]["version"], versions[-1]["uri"])
        assert record["number"] == answer["number"] == 1

        # A checksum this registry never anchored, where the registry itself exists.
        assert "error" in get(f"{service}/registries/{registry.address}/records/nope", expect=404)

    async def test_one_checksum_answers_across_every_registry(self, w3, factory, service, tidx):
        """The lookup no per-registry path can serve: the module's `records(0, checksum, …)`.

        `RecordAdded` indexes the checksum hash, so the same checksum in two registries is one
        topic under two emitters, kept apart by the address each was added under. The status is
        set in one of them only, so a fold across registries shows.
        """
        # A checksum of this test's own: the lookup is over the whole chain rather than one
        # factory's registries, so anything else anchoring the same one is a real answer.
        checksum = "cross-registry-lookup"
        creator = await funded(w3)
        a = await factory.deploy(creator, "a")
        b = await factory.deploy(creator, "b")
        await a.add_record(creator, checksum, uri="ipfs://in-a", metadata='{"where":"a"}')
        await b.add_record(creator, checksum, uri="ipfs://in-b", metadata='{"where":"b"}')
        await b.add_record(creator, "elsewhere", uri="ipfs://other")
        tidx.bounded(await a.set_status(creator, checksum, 1, "approved"))

        answer = get(f"{service}/records/{checksum}")
        assert HexBytes(answer["checksum_hash"]) == HexBytes(keccak(text=checksum))
        assert answer["other"] == 0, "every RecordAdded has its leaf beside it"

        held = {r["registry"].lower(): r for r in answer["records"]}
        assert set(held) == {a.address.lower(), b.address.lower()}, held
        assert held[a.address.lower()]["uri"] == "ipfs://in-a"
        assert held[b.address.lower()]["metadata"] == '{"where":"b"}'
        assert held[a.address.lower()]["status"] == "approved"
        assert held[b.address.lower()]["status"] is None, "one registry's status is not the other's"

        # A checksum nobody anchored is an empty answer rather than an error: unlike a
        # registry, there is no announcement that would have said it exists.
        assert get(f"{service}/records/never-anchored")["records"] == []

    async def test_a_bare_leaf_beside_the_records_is_counted_not_served(self, w3, chain_id, registry, service, tidx):
        """A registry-scoped writer may append a leaf committing to anything -- a record that
        lives off-chain -- and it sits among the record leaves in the same MMR.

        The projections count what does not decode as a record instead of failing on it,
        and say so: a listing that silently dropped a leaf would look like a registry with
        fewer leaves than it has.
        """
        creator = registry.creator
        await registry.add_record(creator, "shared-key", uri="ipfs://mine")
        bare = REG.fns.appendLeaf(b"\xee" * 32, b"not an envelope").data
        tidx.bounded(await send_call(w3, chain_id, creator, registry.address, bare))

        listing = get(f"{service}/registries/{registry.address}/records")
        assert [r["checksum"] for r in listing["records"]] == ["shared-key"], "the bare leaf is not a record"
        assert listing["other"] == 1, "...but it is counted, since silence would hide it"

        answer = get(f"{service}/records/shared-key")
        held = {r["registry"].lower(): r for r in answer["records"]}
        assert set(held) == {registry.address.lower()}
        assert held[registry.address.lower()]["uri"] == "ipfs://mine"
        assert answer["other"] == 0, "no RecordAdded without its leaf"

    async def test_an_address_the_factory_never_deployed_is_not_found(self, factory, registry, service, tidx):
        """The module's "registry 999 does not exist", restored where the log can still say it:
        an address is a registry only because the factory announced it.

        404 rather than an empty list, which would be indistinguishable from a registry with
        nothing in it -- including for the factory's own address, a real contract here.
        """
        tidx.bounded(registry.deployment)

        for address in (factory.address, "0x" + "ad" * 20):
            for path in ("records", "roles", "records/abc"):
                assert "error" in get(f"{service}/registries/{address}/{path}", expect=404)
        assert len(roles_of(service, registry)) == 1, "...while the registry beside it answers"

    async def test_several_registries_records_come_back_in_one_answer(self, w3, factory, service, tidx):
        """The bulk form ``reconcile`` reads with: many registries in one walk.

        Unnumbered, like the cross-registry lookup: a number is a walk of one registry's
        ordering, and the caller this exists for never reads it. An address the factory
        never announced fails the whole request by name, and no addresses is an empty
        answer rather than a walk of every namespace there is.
        """
        creator = await funded(w3)
        a = await factory.deploy(creator, "a")
        b = await factory.deploy(creator, "b")
        await a.add_record(creator, "in-a")
        tidx.bounded(await b.add_record(creator, "in-b"))

        answer = post(f"{service}/registries/records", [a.address, b.address])
        held = {address.lower(): [r["checksum"] for r in records] for address, records in answer["registries"].items()}
        assert held == {a.address.lower(): ["in-a"], b.address.lower(): ["in-b"]}, answer
        assert all(r["number"] is None for records in answer["registries"].values() for r in records), "unnumbered"

        assert post(f"{service}/registries/records", [])["registries"] == {}
        stranger = "0x" + "ad" * 20
        assert "error" in post(f"{service}/registries/records", [a.address, stranger], expect=404)

    async def test_an_empty_registry_is_an_empty_list_and_not_an_error(self, registry, service, tidx):
        tidx.bounded(registry.deployment)

        assert records_of(service, registry) == []
        # Deployment announces the creator's admin, so roles is never empty.
        assert len(roles_of(service, registry)) == 1

    async def test_paging_walks_past_one_round_trip(self, w3, factory, paged_service, tidx):
        """Every projection, fetched two rows at a time, still answers in full.

        The cursor is what carries a page boundary, and at the real page size
        none of these would reach one. A short answer here is the whole failure
        mode: it looks exactly like a registry with less in it.
        """
        creator = await funded(w3)
        registry = await factory.deploy(creator, "docs")
        for checksum in ("a", "b", "c", "d", "e"):
            await registry.add_record(creator, checksum)
        for extra in ("photos", "notes", "audio"):
            await factory.deploy(creator, extra)
        tidx.bounded(await registry.add_record(creator, "f"))

        records = records_of(paged_service, registry)
        assert [r["checksum"] for r in records] == ["a", "b", "c", "d", "e", "f"]
        # Numbered across pages, not restarted at each one.
        assert [r["number"] for r in records] == [1, 2, 3, 4, 5, 6]

        listed = get(f"{paged_service}/registries")["registries"]
        assert [r["name"] for r in listed] == ["docs", "photos", "notes", "audio"]
        assert [r["number"] for r in listed] == [1, 2, 3, 4]

        # Roles pages on its own key, and deployment grants one admin per registry.
        roles = roles_of(paged_service, registry)
        assert [r["role"] for r in roles] == ["admin"]
