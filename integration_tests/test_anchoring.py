"""Anchoring precompile (IAnchoring, 0x…0a00): a caller-partitioned commitment log.

Enshrined from the T10 hardfork, so any EOA anchors under its own address with nothing to
deploy. State is one word per ``(namespace, key)``; history lives in the ``Anchored`` log,
and re-writing the stored commitment reverts.

``TestThroughAnIndexer`` closes the loop on that: history is only in the log if something
can get it back out. Needs ``--tidx``, skips without it.
"""

from collections import namedtuple

import pytest
from eth_utils import keccak
from hexbytes import HexBytes

from .abi import ANCHORING as A
from .abi import ANCHORING_ADDRESS as ADDR
from .anchoring import ANCHORED, ANCHORED_EVENT, ANCHORED_TOPIC, decode_payload, latest
from .tidx import bytea
from .utils import (
    SET_CODE_GAS,
    call_forwarder,
    call_revert,
    deploy_contract,
    error_selector,
    fund,
    new_account,
    send_call,
    send_calls,
    send_set_code_tx,
    staticcall_probe,
)

pytestmark = pytest.mark.tempo  # tempo 0x76 tx, gas in PATH_USD

ZERO32 = b"\x00" * 32

SSTORE_CREATE_COST = 250_000  # TIP-1000: creating a new state element, as in `test_gas`

# The whole ABI the x/anchoring precompile served at this address, with the selectors its spec
# pinned -- all of it calldata a stale integration might still send. The value is written out
# beside each signature because a mistyped signature would hash to something and then agree
# with itself.
RETIRED = {
    "addRegistry(string,string,string)": "318b38b1",
    "addRecord((string,string,string,string,string,string,uint64,uint64,bool,uint64))": "64d25295",
    "updateRecordStatus(uint64,uint64,uint64,string)": "97b40c25",
    "grantRole(uint64,string,address,string)": "b8fdd1a7",
    "revokeRole(uint64,string,address,string)": "acd58bc7",
    "records(uint64,string,uint64,uint64,(bytes,uint64,uint64,bool,bool))": "c7be5e37",
    "registries(uint64,(bytes,uint64,uint64,bool,bool))": "17bd3e65",
}

COMMITMENT_UNCHANGED = error_selector("CommitmentUnchanged()")
UNKNOWN_FUNCTION_SELECTOR = error_selector("UnknownFunctionSelector(bytes4)")

Anchored = namedtuple("Anchored", "caller key commitment metadata")


def key_of(label: str) -> bytes:
    """Applications derive their own keys; a label digest is the simplest such scheme."""
    return keccak(text=label)


async def anchor(w3, chain_id, signer, key, commitment, metadata=b""):
    return await send_call(w3, chain_id, signer, ADDR, A.fns.anchor(key, commitment, metadata).data)


async def anchor_and_hash(w3, chain_id, signer, key, metadata):
    return await send_call(w3, chain_id, signer, ADDR, A.fns.anchorAndHash(key, metadata).data)


def anchored(receipt) -> list[Anchored]:
    """The receipt's ``Anchored`` events, decoded, in canonical log order."""
    return [
        Anchored(
            "0x" + bytes(lg["topics"][1])[-20:].hex(),
            bytes(lg["topics"][2]),
            *decode_payload(lg["data"]),
        )
        for lg in receipt["logs"]
        if lg["address"].lower() == ADDR.lower() and lg["topics"][0] == ANCHORED_TOPIC
    ]


# Init code that anchors from the *constructor* and deploys nothing: the call runs while the
# address making it still holds no code. The arguments trail the prefix instead of being built
# on the stack, so any calldata short enough for a PUSH1 length can be sent.
#
#   codecopy(0, <len(prefix)>, len)           ; the arguments follow this code
#   call(gas, <precompile>, 0, 0, len, 0, 0)
#   return(0, 0)                              ; no runtime
_CONSTRUCTOR_ANCHOR = "60{len} 60{off} 6000 39  6000 6000 60{len} 6000 6000  73{addr} 5a f1 50  6000 6000 f3"


def _constructor_anchor(precompile: str, calldata: bytes) -> bytes:
    """Init code calling ``precompile`` with ``calldata`` from its own constructor."""
    assert len(calldata) < 0x100, "the arguments have to fit a PUSH1 length"
    fields = {"addr": precompile.removeprefix("0x"), "len": f"{len(calldata):02x}", "off": "00"}
    # Measured rather than written down: the offset the arguments sit at is the prefix's
    # own length, which changes with the code above it.
    prefix = bytes.fromhex(_CONSTRUCTOR_ANCHOR.format(**fields))
    return bytes.fromhex(_CONSTRUCTOR_ANCHOR.format(**fields | {"off": f"{len(prefix):02x}"})) + calldata


@pytest.fixture
async def anchorer(w3):
    """A funded EOA; its own address is the namespace it writes under."""
    account = new_account()
    await fund(w3, account.address)
    return account


class TestReads:
    async def test_precompile_is_enshrined(self, w3):
        """Installed at the T10 boundary rather than deployed, so it carries only the marker byte."""
        assert bytes(await w3.eth.get_code(ADDR)) == b"\xef"

    async def test_untouched_key_reads_zero(self, w3, anchorer):
        assert await latest(w3, anchorer.address, key_of("never-anchored")) == ZERO32

    async def test_head_slot_derivation_is_reproducible_off_chain(self, w3, chain_id, anchorer):
        """An indexer derives ``headSlot(ns, key) = keccak256(0x01 ‖ pad32(ns) ‖ key)`` itself.

        Read back through ``eth_getStorageAt``, against the node's storage rather than the code.
        """
        key, commitment = key_of("indexed"), b"\xc0" * 32
        await anchor(w3, chain_id, anchorer, key, commitment)

        preimage = b"\x01" + bytes(12) + bytes.fromhex(anchorer.address[2:]) + key
        slot = int.from_bytes(keccak(preimage), "big")

        assert bytes(await w3.eth.get_storage_at(ADDR, slot)) == commitment
        assert await latest(w3, anchorer.address, key) == commitment


class TestAnchor:
    async def test_anchor_sets_head_and_emits(self, w3, chain_id, anchorer):
        key, commitment, metadata = key_of("doc-1"), b"\x11" * 32, b'{"v":1,"kind":"content"}'

        receipt = await anchor(w3, chain_id, anchorer, key, commitment, metadata)

        assert await latest(w3, anchorer.address, key) == commitment
        (event,) = anchored(receipt)
        assert event.caller.lower() == anchorer.address.lower()
        assert (event.key, event.commitment, event.metadata) == (key, commitment, metadata)

    async def test_successive_anchors_update_head_and_emit_each(self, w3, chain_id, anchorer):
        """Version order lives in the log; the chain keeps only the head."""
        key, first, second = key_of("doc-2"), b"\x22" * 32, b"\x33" * 32

        for commitment in (first, second):
            receipt = await anchor(w3, chain_id, anchorer, key, commitment)
            assert [e.commitment for e in anchored(receipt)] == [commitment]
            assert await latest(w3, anchorer.address, key) == commitment

    async def test_older_commitment_can_be_anchored_again(self, w3, chain_id, anchorer):
        """Only the *current* head is rejected; going back is a genuine state change."""
        key, first, second = key_of("doc-4"), b"\x55" * 32, b"\x66" * 32

        for commitment in (first, second, first):
            await anchor(w3, chain_id, anchorer, key, commitment)

        assert await latest(w3, anchorer.address, key) == first

    async def test_zero_commitment_is_a_reset_not_a_special_case(self, w3, chain_id, anchorer):
        """Zero is a legal commitment; it is rejected only when the head is already zero."""
        key = key_of("doc-5")
        data = A.fns.anchor(key, ZERO32, b"").data

        assert COMMITMENT_UNCHANGED in await call_revert(w3, ADDR, data, sender=anchorer.address)

        await anchor(w3, chain_id, anchorer, key, b"\x77" * 32)
        await anchor(w3, chain_id, anchorer, key, ZERO32)
        assert await latest(w3, anchorer.address, key) == ZERO32

    async def test_re_anchoring_current_commitment_reverts(self, w3, chain_id, anchorer):
        """The no-op rule — and it is the commitment that matters, not the metadata."""
        key, commitment = key_of("doc-3"), b"\x44" * 32
        await anchor(w3, chain_id, anchorer, key, commitment, b"first")
        data = A.fns.anchor(key, commitment, b"different").data

        assert COMMITMENT_UNCHANGED in await call_revert(w3, ADDR, data, sender=anchorer.address)

        # ...and as a real tx it fails, leaving the head and the log untouched.
        receipt = await send_calls(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), calls=[{"to": ADDR, "data": data}]
        )
        assert receipt["status"] == 0
        assert not anchored(receipt)
        assert await latest(w3, anchorer.address, key) == commitment


class TestAnchorAndHash:
    async def test_anchor_and_hash_commits_metadata_digest(self, w3, chain_id, anchorer):
        """The event is self-verifying: the commitment is the digest of its own metadata."""
        key, metadata = key_of("doc-6"), b'{"v":1,"kind":"content","data":{"uri":"ipfs://QmX"}}'

        receipt = await anchor_and_hash(w3, chain_id, anchorer, key, metadata)

        (event,) = anchored(receipt)
        assert event.commitment == keccak(metadata)
        assert keccak(event.metadata) == event.commitment
        assert await latest(w3, anchorer.address, key) == event.commitment

    async def test_anchor_and_hash_replays_rows_differing_only_in_seq(self, w3, chain_id, anchorer):
        """Legacy replay: two rows identical but for their ordinal must both land.

        Hashing the envelope rather than the raw checksum is what makes this work — the ``seq``
        discriminator changes the digest, so the second write is not a no-op.
        """
        key = key_of("doc-7")
        for seq in (1, 2):
            await anchor_and_hash(w3, chain_id, anchorer, key, b'{"seq":%d,"checksum":"0xabc"}' % seq)

        assert await latest(w3, anchorer.address, key) == keccak(b'{"seq":2,"checksum":"0xabc"}')


class TestNamespaces:
    async def test_namespaces_are_isolated(self, w3, chain_id, anchorer):
        """Writes land under msg.sender, so one account cannot reach another's partition."""
        other = new_account()
        await fund(w3, other.address)
        key, mine, theirs = key_of("shared-key"), b"\x88" * 32, b"\x99" * 32

        await anchor(w3, chain_id, anchorer, key, mine)
        await anchor(w3, chain_id, other, key, theirs)
        assert await latest(w3, anchorer.address, key) == mine
        assert await latest(w3, other.address, key) == theirs

        # The no-op rule is per namespace: mirroring another account's commitment is a real write.
        await anchor(w3, chain_id, other, key, mine)
        assert await latest(w3, anchorer.address, key) == mine
        assert await latest(w3, other.address, key) == mine

    async def test_a_contract_anchors_under_its_own_address(self, w3, chain_id, anchorer):
        """The namespace is msg.sender, not tx.origin -- which is what makes a wrapper contract
        a partition rather than a way into its caller's."""
        key, commitment = key_of("via-contract"), b"\xab" * 32
        _, forwarder = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=call_forwarder(ADDR)
        )

        receipt = await send_call(w3, chain_id, anchorer, forwarder, A.fns.anchor(key, commitment, b"").data)

        assert await latest(w3, forwarder, key) == commitment
        assert await latest(w3, anchorer.address, key) == ZERO32, "the sending EOA's namespace is untouched"
        (event,) = anchored(receipt)
        assert event.caller.lower() == forwarder.lower(), "the log names the contract, not the origin"

    async def test_a_delegated_eoa_anchors_under_its_own_address(self, w3, chain_id, anchorer):
        """EIP-7702: the code is borrowed, the namespace is not.

        Every account delegating to one smart-account implementation shares its code, so a
        namespace taken from there would put all of them in one partition.
        """
        key, commitment = key_of("delegated"), b"\xcd" * 32
        _, delegate = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=call_forwarder(ADDR)
        )
        authority = new_account()  # fresh EOA: no code of its own, and no anchors

        # One tx installs the delegation and calls into it, so the forwarder's CALL is made
        # from `authority` -- the sponsor supplies the transaction and pays for it.
        receipt = await send_set_code_tx(
            w3,
            chain_id=chain_id,
            sponsor=anchorer,
            authority=authority,
            delegate=delegate,
            auth_nonce=0,
            to=authority.address,
            data=bytes(A.fns.anchor(key, commitment, b"").data),
            gas=SET_CODE_GAS + SSTORE_CREATE_COST,  # the anchor creates a head slot
        )

        assert receipt["status"] == 1
        assert await latest(w3, authority.address, key) == commitment
        assert await latest(w3, delegate, key) == ZERO32, "not the implementation it borrowed"
        assert await latest(w3, anchorer.address, key) == ZERO32, "nor the sponsor's"

    async def test_a_constructor_anchors_under_the_address_being_created(self, w3, chain_id, anchorer):
        """The x/anchoring module refused a constructor call outright; here it is a caller like
        any other, under the address being created -- which holds no code yet, and never
        will."""
        key, commitment = key_of("from-a-constructor"), b"\xbc" * 32
        initcode = _constructor_anchor(ADDR, bytes(A.fns.anchor(key, commitment, b"").data))

        _, created = await deploy_contract(w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=initcode)

        assert bytes(await w3.eth.get_code(created)) == b"", "the constructor returns no runtime"
        assert await latest(w3, created, key) == commitment
        assert await latest(w3, anchorer.address, key) == ZERO32, "not the deployer's namespace"


class TestRefusedCalls:
    async def test_value_bearing_anchor_is_rejected(self, w3, chain_id, anchorer):
        """Refused by ``eth_call``, and refused again at admission as a real tx.

        There is no native token here, so a value-bearing call never reaches a block: the
        balance the x/anchoring suite watched has nothing to say, and the head is the whole
        record of what the attempt left behind.
        """
        key = key_of("paid")
        data = A.fns.anchor(key, b"\xaa" * 32, b"").data
        tx = {
            "to": ADDR,
            "from": anchorer.address,
            "data": "0x" + bytes(data).hex(),
            "gas": hex(2_000_000),
            "value": "0x1",
        }

        resp = await w3.provider.make_request("eth_call", [tx, "latest"])
        assert resp.get("error") is not None, f"value-bearing anchor must fail, got {resp!r}"

        with pytest.raises(Exception, match="value transfer not allowed"):
            await send_calls(
                w3, chain_id=chain_id, private_key=anchorer.key.hex(), calls=[{"to": ADDR, "value": 1, "data": data}]
            )
        assert await latest(w3, anchorer.address, key) == ZERO32

    async def test_writes_are_rejected_in_a_read_only_frame(self, w3, chain_id, anchorer):
        """Reads serve a STATICCALL; writes refuse it. ``eth_call`` cannot make a read-only
        frame, so a deployed probe does."""
        key, commitment = key_of("static"), b"\xd0" * 32
        await anchor(w3, chain_id, anchorer, key, commitment)

        _, probe = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=staticcall_probe(ADDR)
        )

        async def succeeds(data) -> bool:
            out = await w3.eth.call({"to": probe, "data": data})
            return int.from_bytes(bytes(out), "big") == 1

        assert await succeeds(A.fns.latest(anchorer.address, key).data), "latest must serve a staticcall"
        assert not await succeeds(A.fns.anchor(key, b"\xd1" * 32, b"").data), "anchor must refuse one"
        assert not await succeeds(A.fns.anchorAndHash(key, b"x").data), "anchorAndHash must refuse one"

        assert await latest(w3, anchorer.address, key) == commitment, "the head is untouched"

    async def test_malformed_calldata_reverts(self, w3, anchorer):
        """A known selector with truncated arguments fails the ABI decode, before any state."""
        truncated = "0x" + bytes(A.fns.anchor(key_of("doc"), b"\x11" * 32, b"").data)[:20].hex()

        await call_revert(w3, ADDR, truncated, sender=anchorer.address)
        assert await latest(w3, anchorer.address, key_of("doc")) == ZERO32

    @pytest.mark.parametrize("signature", list(RETIRED), ids=lambda s: s.split("(")[0])
    async def test_retired_selectors_revert_unknown_function_selector(self, w3, anchorer, signature):
        """The address is reused, the ABI is not: old calldata fails loudly instead of
        mis-decoding. Every retired function, because one that still answered would read a
        registry id out of the first word and write somewhere."""
        selector = error_selector(signature)
        assert selector == RETIRED[signature], f"{signature} no longer hashes to its pinned selector"

        err = await call_revert(w3, ADDR, "0x" + selector + "00" * 64, sender=anchorer.address)

        assert UNKNOWN_FUNCTION_SELECTOR in err
        assert selector in err, "the rejected selector is echoed back"


class TestGas:
    """What an anchor costs: an estimate that covers the receipt, the storage a first write
    creates, and a payload that is paid for. The x/anchoring suite watched all three."""

    async def test_estimate_covers_the_anchor_it_estimated(self, w3, chain_id, anchorer):
        """The first write under a key and the one after it differ by an order of magnitude,
        so an estimate covering only the cheap one would be an out-of-gas tx."""
        key = key_of("estimated")

        for commitment in (b"\x01" * 32, b"\x02" * 32):
            data = A.fns.anchor(key, commitment, b"").data
            estimate = await w3.eth.estimate_gas({"from": anchorer.address, "to": ADDR, "data": data})

            receipt = await anchor(w3, chain_id, anchorer, key, commitment)

            assert receipt["gasUsed"] <= estimate, f"estimate {estimate} short of {receipt['gasUsed']}"

    async def test_the_first_anchor_under_a_key_pays_for_the_slot(self, w3, chain_id, anchorer):
        """TIP-1000: a head is metered storage, so creating one costs ~250k more than moving
        it -- which is also what says ``latest`` reads state rather than the log."""
        await anchor(w3, chain_id, anchorer, key_of("warm"), b"\x01" * 32)  # this pays account creation
        key = key_of("priced")

        creating = await anchor(w3, chain_id, anchorer, key, b"\x11" * 32)
        updating = await anchor(w3, chain_id, anchorer, key, b"\x22" * 32)

        delta = creating["gasUsed"] - updating["gasUsed"]
        assert SSTORE_CREATE_COST * 0.94 <= delta <= SSTORE_CREATE_COST, delta

    async def test_the_metadata_is_charged_for(self, w3, chain_id, anchorer):
        """The payload is only logged, never stored -- but still paid for, at no less than its
        own calldata costs. Otherwise the log would be a free channel."""
        key, extra = key_of("payloads"), 1024
        await anchor(w3, chain_id, anchorer, key, b"\x01" * 32, b"x" * 32)  # the slot now exists

        small = await anchor(w3, chain_id, anchorer, key, b"\x02" * 32, b"x" * 32)
        big = await anchor(w3, chain_id, anchorer, key, b"\x03" * 32, b"x" * (32 + extra))

        delta = big["gasUsed"] - small["gasUsed"]
        assert delta >= 16 * extra, f"{extra} more bytes cost {delta}, under the calldata floor"


def heads_sql(up_to: int) -> str:
    """Mirrors ``heads_sql`` in nvnmchain-anchoring's ``src/tidx.rs``: the precompile's own
    rule as SQL, one word per ``(namespace, key)``, newest anchor wins. Over the base
    ``logs`` table for the reason the second test pins, and bounded so a head cannot come
    from a block the state read did not cover.

    The one query still written twice. The registry projections are checked through the
    service that owns them, but nothing serves heads over HTTP -- the audit reads them and
    keeps them to itself -- so there is no endpoint to check this rule through."""
    return (
        "SELECT namespace, key, data FROM ("
        " SELECT topic1 AS namespace, topic2 AS key, data,"
        " ROW_NUMBER() OVER (PARTITION BY topic1, topic2"
        " ORDER BY block_num DESC, log_idx DESC) AS rn"
        f" FROM logs WHERE address = {bytea(ADDR)} AND selector = {bytea(ANCHORED.topic)}"
        f" AND block_num <= {up_to}"
        ") heads WHERE rn = 1"
    )


class TestThroughAnIndexer:
    """History and payloads are left to the log "for indexers to derive", so the design
    holds only if one can. These run the query an indexer uses, where every way of being
    wrong returns rows that parse and an empty head set audits clean. Needs ``--tidx``."""

    async def test_heads_query_agrees_with_the_precompile(self, w3, chain_id, anchorer, tidx):
        """Every head the query derives is the word the chain kept.

        Two keys, one anchored twice, so both halves of the rule are covered: the
        partition keeps the keys apart, and the ordering picks the newer row.
        """
        stale, head = b"\x11" * 32, b"\x22" * 32
        versioned, single = key_of("tidx-versioned"), key_of("tidx-single")

        await anchor(w3, chain_id, anchorer, versioned, stale, b'{"v":1}')
        await anchor(w3, chain_id, anchorer, single, head, b"solo")
        receipt = await anchor(w3, chain_id, anchorer, versioned, head, b'{"v":2}')
        at = tidx.bounded(receipt)

        ours = {
            HexBytes(row["key"]): decode_payload(row["data"])
            for row in tidx.sql(heads_sql(at))
            if HexBytes(row["namespace"])[-20:] == HexBytes(anchorer.address)
        }
        assert set(ours) == {HexBytes(versioned), HexBytes(single)}, f"got {list(ours)}"

        for key, (commitment, _) in ours.items():
            assert commitment == await latest(w3, anchorer.address, key), f"head disagrees for {key.hex()}"
        assert ours[HexBytes(versioned)] == (head, b'{"v":2}'), "the newest anchor wins, payload and all"

    async def test_metadata_does_not_survive_tidx_decoding(self, w3, chain_id, anchorer, tidx):
        """Why the query above reads raw ``data`` instead of a decoded event table.

        tidx decodes a dynamic ``bytes`` argument as the 32-byte head word -- the ABI
        offset, not the payload -- while ``string`` gets a real dereference. An indexer
        trusting that column would hash ``0x…40`` for every anchor. Pinned rather than
        worked around silently: if tidx grows the dereference this fails, and the raw
        read can go.
        """
        key, commitment, metadata = key_of("tidx-metadata"), b"\x33" * 32, b"payload-not-offset"
        receipt = await anchor(w3, chain_id, anchorer, key, commitment, metadata)

        (row,) = tidx.sql(
            f"SELECT commitment, metadata FROM Anchored WHERE key = {bytea(key)}"
            f" AND block_num <= {tidx.bounded(receipt)}",
            signature=ANCHORED_EVENT,
        )

        assert HexBytes(row["commitment"]) == commitment, "a fixed-width argument decodes correctly"
        assert HexBytes(row["metadata"]) != metadata, "if this now holds, tidx decodes dynamic bytes"
        assert int.from_bytes(bytes(HexBytes(row["metadata"])), "big") == 64, "it is the ABI offset word"
