"""nvnmchain-anchoring's HTTP service, against a real chain and a real index.

The projections it serves are the ones this suite used to re-implement in Python and query
directly. These tests run *its* implementation instead, against the same oracle that SQL was
checked by: what ``/roles`` serves is what ``hasRole`` answers. Nothing is lost by not writing
the query twice, and these are now the only tests over the projections.

Needs ``--tidx`` and the service binary; ``binary`` says what a missing one does.
"""

import gzip
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest
from eth_utils import keccak
from hexbytes import HexBytes

from .network import _resolve_bin, free_port
from .registry import ADMIN, EDITOR, deployed_address
from .utils import STATE_WRITE_GAS, funded, new_account, send_calls

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
    """A mainnet-full-export subset of one registry, as the migration reads it: the tranche
    file on disk, the manifest that has to match it byte for byte, and the registry listing."""
    directory.mkdir(parents=True, exist_ok=True)
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
    (directory / "registries.json").write_text(
        json.dumps([{"name": name, "description": "the docs", "metadata": "{}"}])
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "totals": {"registries": 1, "records": len(lines)},
                "files": [
                    {
                        "registry": name,
                        "records": len(lines),
                        "file": f"{name}.jsonl.gz",
                        "tranche": 1,
                        "sha256_gz": hashlib.sha256(archive).hexdigest(),
                        "sha256_uncompressed": hashlib.sha256(content).hexdigest(),
                    }
                ],
            }
        )
    )
    return str(directory)


def get(url: str, *, expect: int = 200) -> dict:
    """A JSON GET, asserting the status. A failure here is a JSON body with an
    ``error`` key rather than an empty result, which is the service's contract."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed localhost URL
            body, status = json.load(resp), resp.status
    except urllib.error.HTTPError as e:
        body, status = json.load(e), e.code
    assert status == expect, f"{url} -> {status}: {body}"
    return body


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
    """One projection from the command line: the JSON it printed, or its message when it
    was asked for something it does not have. ``raw`` for a plan, which is JSON per line."""
    proc = subprocess.run(  # noqa: S603 - a binary this suite resolved itself
        [binary(), *args],
        env=settings(tempo, tidx, factory),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == expect, f"{args} -> {proc.returncode}: {proc.stderr}"
    if expect != 0:
        return proc.stderr
    return proc.stdout if raw else json.loads(proc.stdout)


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
        steps = [
            json.loads(line)
            for line in cli(
                tempo,
                tidx,
                factory,
                "migrate",
                f"--registries={export}/registries.json",
                f"--manifest={export}/manifest.json",
                f"--export={export}",
                "--threshold=100",
                raw=True,
            ).splitlines()
        ]
        assert [s["kind"] for s in steps] == ["deploy", "record", "record", "status", "record"]

        # Sent exactly as planned, in order: the deploy names the registry the rest target.
        creator, deployed = await funded(w3), {}

        async def send(those):
            receipt = None
            for step in those:
                to = factory.address if step["kind"] == "deploy" else deployed[step["registry"]]
                receipt = await send_calls(
                    w3,
                    chain_id=chain_id,
                    private_key=creator.key.hex(),
                    calls=[{"to": to, "data": bytes.fromhex(step["data"][2:])}],
                    gas_limit=STATE_WRITE_GAS,
                )
                assert receipt["status"] == 1, step
                if step["kind"] == "deploy":
                    deployed[step["registry"]] = deployed_address(receipt, factory.address)
            tidx.bounded(receipt)

        plan = tmp_path / "plan.jsonl"
        plan.write_text("\n".join(json.dumps(step) for step in steps))

        # A run that stops halfway resumes from what the chain holds, never from how far it
        # got: `addRecord` appends every time, so re-sending a landed step would leave a
        # version too many rather than doing nothing.
        await send(steps[:2])
        left = tmp_path / "left.jsonl"
        message = cli(tempo, tidx, factory, "reconcile", f"--plan={plan}", f"--remaining={left}", expect=1)
        assert "step(s) still to send" in message, message
        await send([json.loads(line) for line in left.read_text().splitlines()])

        assert cli(tempo, tidx, factory, "reconcile", f"--plan={plan}") == []

    async def test_a_malformed_address_is_refused_rather_than_queried(self, service):
        # A filtered address would query a real-looking other one and answer
        # "nothing here" -- the quiet wrong answer this crate exists to notice.
        for bad in ["notanaddress", "0x1234", "0x" + "aa" * 21]:
            body = get(f"{service}/registries/{bad}/roles", expect=400)
            assert "error" in body, body

    async def test_a_records_versions_come_back_from_the_log(self, registry, service, tidx):
        """History is the half of a record the chain does not keep: state is one word per key,
        so every version but the newest exists only as the log row the head replaced.

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

        # The listing still answers at the head, and the two agree on which that is -- and on
        # the number, which both pay a walk of the registry's ordering for.
        (record,) = [r for r in records_of(service, registry) if r["checksum"] == "abc"]
        assert (record["version"], record["uri"]) == (versions[-1]["version"], versions[-1]["uri"])
        assert record["number"] == answer["number"] == 1

        # A checksum this registry never anchored, where the registry itself exists.
        assert "error" in get(f"{service}/registries/{registry.address}/records/nope", expect=404)

    async def test_one_checksum_answers_across_every_registry(self, w3, factory, service, tidx):
        """The lookup no per-registry path can serve: the module's `records(0, checksum, …)`.

        A record's key derives from its checksum and nothing else, so the same checksum in two
        registries is one key under two namespaces, kept apart by the address each was anchored
        under. The status is anchored in one of them only, so a fold across namespaces shows.
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
        assert answer["other"] == 0, "nothing else is anchored under this key"

        held = {r["registry"].lower(): r for r in answer["records"]}
        assert set(held) == {a.address.lower(), b.address.lower()}, held
        assert held[a.address.lower()]["uri"] == "ipfs://in-a"
        assert held[b.address.lower()]["metadata"] == '{"where":"b"}'
        assert held[a.address.lower()]["status"] == "approved"
        assert held[b.address.lower()]["status"] is None, "one registry's status is not the other's"

        # A checksum nobody anchored is an empty answer rather than an error: unlike a
        # registry, there is no announcement that would have said it exists.
        assert get(f"{service}/records/never-anchored")["records"] == []

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
