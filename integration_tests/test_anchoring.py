"""Anchoring precompile (IAnchoring, 0x…0a00): a caller-partitioned commitment log.

Enshrined from the T9 hardfork, so any EOA anchors under its own address with nothing to
deploy. State is one word per ``(namespace, key)``; history lives in the ``Anchored`` log,
and re-writing the stored commitment reverts.
"""

from collections import namedtuple

import pytest
from eth_abi.abi import decode
from eth_utils import keccak
from hexbytes import HexBytes

from .abi import ANCHORING as A
from .abi import ANCHORING_ADDRESS as ADDR
from .utils import (
    call_revert,
    deploy_contract,
    error_selector,
    fund,
    new_account,
    send_call,
    send_calls,
)

pytestmark = pytest.mark.tempo  # tempo 0x76 tx, gas in PATH_USD

ANCHORED_TOPIC = HexBytes(keccak(text="Anchored(address,bytes32,bytes32,bytes)"))
ZERO32 = b"\x00" * 32

# The paginated record query the x/anchoring precompile served at this address; the tuple is
# its PageRequest. Any retired selector would do; this is calldata a stale integration sends.
LEGACY_SELECTOR = error_selector("records(uint64,string,uint64,uint64,(bytes,uint64,uint64,bool,bool))")

COMMITMENT_UNCHANGED = error_selector("CommitmentUnchanged()")
UNKNOWN_FUNCTION_SELECTOR = error_selector("UnknownFunctionSelector(bytes4)")

Anchored = namedtuple("Anchored", "caller key commitment metadata")


def key_of(label: str) -> bytes:
    """Applications derive their own keys; a label digest is the simplest such scheme."""
    return keccak(text=label)


async def latest(w3, namespace, key) -> bytes:
    return bytes(await A.fns.latest(namespace, key).call(w3, to=ADDR))


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
            *decode(["bytes32", "bytes"], bytes(lg["data"])),
        )
        for lg in receipt["logs"]
        if lg["address"].lower() == ADDR.lower() and lg["topics"][0] == ANCHORED_TOPIC
    ]


# A contract that STATICCALLs the precompile with whatever calldata it is given and answers
# with the success flag, so a write refused in a read-only frame is distinguishable from one
# that returned a zero word. `test_precompiles` has a forwarder that returns the callee's
# output instead, which cannot tell those apart.
#
#   calldatacopy(0, 0, calldatasize)          ; the call's data becomes the staticcall's args
#   staticcall(gas, <precompile>, 0, cds, 0, 0)
#   mstore(0, success) ; return(0, 32)
_PROBE_RUNTIME = "36 6000 6000 37  6000 6000 36 6000  73{addr} 5a fa  6000 52  6020 6000 f3"


def _staticcall_probe(precompile: str) -> bytes:
    """Init code deploying the probe above, pointed at ``precompile``."""
    addr = precompile.removeprefix("0x")
    assert len(addr) == 40, "precompile must be a 20-byte address"
    runtime = bytes.fromhex(_PROBE_RUNTIME.format(addr=addr))
    # Constructor: copy the runtime out from past this 12-byte prefix, then return it.
    return bytes.fromhex(f"60{len(runtime):02x} 600c 6000 39 60{len(runtime):02x} 6000 f3") + runtime


@pytest.fixture
async def anchorer(w3):
    """A funded EOA; its own address is the namespace it writes under."""
    account = new_account()
    await fund(w3, account.address)
    return account


class TestReads:
    async def test_precompile_is_enshrined(self, w3):
        """Installed at the T9 boundary rather than deployed, so it carries only the marker byte."""
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


class TestRefusedCalls:
    async def test_value_bearing_anchor_is_rejected(self, w3, anchorer):
        data = A.fns.anchor(key_of("paid"), b"\xaa" * 32, b"").data
        tx = {
            "to": ADDR,
            "from": anchorer.address,
            "data": data if isinstance(data, str) else "0x" + bytes(data).hex(),
            "gas": hex(2_000_000),
            "value": "0x1",
        }

        resp = await w3.provider.make_request("eth_call", [tx, "latest"])
        assert resp.get("error") is not None, f"value-bearing anchor must fail, got {resp!r}"
        assert await latest(w3, anchorer.address, key_of("paid")) == ZERO32

    async def test_writes_are_rejected_in_a_read_only_frame(self, w3, chain_id, anchorer):
        """Reads serve a STATICCALL; writes refuse it. ``eth_call`` cannot make a read-only
        frame, so a deployed probe does."""
        key, commitment = key_of("static"), b"\xd0" * 32
        await anchor(w3, chain_id, anchorer, key, commitment)

        _, probe = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=_staticcall_probe(ADDR)
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

    async def test_legacy_selector_reverts_unknown_function_selector(self, w3, anchorer):
        """The address is reused, the ABI is not: old calldata fails loudly instead of mis-decoding."""
        err = await call_revert(w3, ADDR, "0x" + LEGACY_SELECTOR + "00" * 64, sender=anchorer.address)

        assert UNKNOWN_FUNCTION_SELECTOR in err
        assert LEGACY_SELECTOR in err, "the rejected selector is echoed back"
