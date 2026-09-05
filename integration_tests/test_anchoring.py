"""Anchoring precompile (IAnchoring, 0x…0a00): one Merkle Mountain Range per caller.

Enshrined from the T10 hardfork, so any EOA appends under its own address with nothing to
deploy. State is the leaf count and one peak per height; payloads live only in the log, with
the peaks, so a proof needs the log and nothing else.

``TestThroughAnIndexer`` closes the loop on that: the leaves are only in the log if something
can fold them back into the root the chain holds. Needs ``--tidx``, skips without it.
"""

import pytest
from eth_utils import keccak
from hexbytes import HexBytes

from .abi import ANCHORING as A
from .abi import ANCHORING_ADDRESS as ADDR
from .anchoring import (
    LEAF_APPENDED,
    LEAF_APPENDED_EVENT,
    Mmr,
    bag,
    batches_of,
    decode_leaf,
    hash_leaf,
    hash_merge,
    leaves_of,
    perfect,
    root,
    state,
)
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

CHUNK_NOT_ALIGNED = error_selector("ChunkNotAligned(uint256,uint256)")
CHUNKS_MISMATCH = error_selector("ChunksMismatch()")
UNKNOWN_FUNCTION_SELECTOR = error_selector("UnknownFunctionSelector(bytes4)")

# The sixteen roots the precompile's own suite and the contracts' pin, over the commitments
# ``bytes32(1)``, ``bytes32(2)``, … appended in that order -- computed independently, so the
# chain has to reproduce them rather than agree with itself.
ROOTS = [
    bytes.fromhex(h)
    for h in (
        "5786039c2502cb1b5ff9a9f0b0b6957bb8b3f6489d20080f677236b2dd590dcd",
        "9950fe45570c3e4c9c0241de506d53ba63bb5b4ceb7b3c0032148e32f1ab3d9d",
        "036e11a04c28d071bc9b3961be683ff7eac4aad9234b6a21904de44b952cb3c9",
        "9a444d98cfab773b89efcfe3749342cd1b072e8f2276f9f822fb1e19edabb77b",
        "bbd0ad9fcc22a20f7adc962f214aba7710aed4d06063e7d722d65d07920a269d",
        "950d9243a18618ebce2f7906ead2e5c9cfe719359d7b8635cf52ee4995c53631",
        "237757481f6015968d2dd6b7784aa544f822d29f6a520bfae222c79c16051c14",
        "2a43055cc8a7bb9202beebc4603c13e920c9c7f7e3bf26ca5178aad751d5b29e",
        "8948ab91036932c2798daf8808b183438f08a6acb56cae4fe3d0db2ff999fd11",
        "0dcc0e544f9c3d0d78a0b030257eb964bc1756e786ede2c565c24817885bee6c",
        "bbe8d27929385c3988405fe38bf7a82136581ef7ea7a2f71634d9785eddaf1d7",
        "d3ebf5629b714dde40059d9dd0bb940d3748ead5953aa63d5d7cc867354b28fa",
        "bc438a6c52d1d3f2abea81fdd299bdfb9c8961b03e2adbeeff075db74971b2ae",
        "d41583f4d63289dafc25e7b5beaefe0f1e453fe2b9f0ba50cdfa96e27689c9fe",
        "7d75dea0b9798ddaa25f8a0d0e6222784f6ad299617a9128e7d75af3bf5eb81e",
        "c60e652673b4bff570b066c5513bf939b9a69b21c5ad6802f3579166b660c2c2",
    )
]


def c(i: int) -> bytes:
    """The commitments the reference roots are over."""
    return i.to_bytes(32, "big")


def chunk(from_: int, size: int) -> tuple[bytes, int]:
    """A perfect subtree over ``c(from_) .. c(from_ + size)``, as ``appendLeaves`` takes it."""
    return perfect([c(from_ + i) for i in range(size)]), size.bit_length() - 1


async def append(w3, chain_id, signer, commitment, metadata=b"", *, to=ADDR):
    return await send_call(w3, chain_id, signer, to, A.fns.appendLeaf(commitment, metadata).data)


async def append_leaves(w3, chain_id, signer, chunks: list[tuple[bytes, int]], metadata=b""):
    roots, heights = [r for r, _ in chunks], [h for _, h in chunks]
    return await send_call(w3, chain_id, signer, ADDR, A.fns.appendLeaves(roots, heights, metadata).data)


async def append_all(w3, chain_id, signer, up_to: int):
    """``c(1) .. c(up_to)`` one by one, returning the receipts."""
    return [await append(w3, chain_id, signer, c(i)) for i in range(1, up_to + 1)]


# Init code that appends from the *constructor* and deploys nothing: the call runs while the
# address making it still holds no code. The arguments trail the prefix instead of being built
# on the stack, so any calldata short enough for a PUSH1 length can be sent.
#
#   codecopy(0, <len(prefix)>, len)           ; the arguments follow this code
#   call(gas, <precompile>, 0, 0, len, 0, 0)
#   return(0, 0)                              ; no runtime
_CONSTRUCTOR_CALL = "60{len} 60{off} 6000 39  6000 6000 60{len} 6000 6000  73{addr} 5a f1 50  6000 6000 f3"


def _constructor_call(precompile: str, calldata: bytes) -> bytes:
    """Init code calling ``precompile`` with ``calldata`` from its own constructor."""
    assert len(calldata) < 0x100, "the arguments have to fit a PUSH1 length"
    fields = {"addr": precompile.removeprefix("0x"), "len": f"{len(calldata):02x}", "off": "00"}
    # Measured rather than written down: the offset the arguments sit at is the prefix's
    # own length, which changes with the code above it.
    prefix = bytes.fromhex(_CONSTRUCTOR_CALL.format(**fields))
    return bytes.fromhex(_CONSTRUCTOR_CALL.format(**fields | {"off": f"{len(prefix):02x}"})) + calldata


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

    async def test_untouched_namespace_reads_empty(self, w3, anchorer):
        assert await root(w3, anchorer.address) == ZERO32
        assert await state(w3, anchorer.address) == (0, [])

    async def test_slot_layout_is_reproducible_off_chain(self, w3, chain_id, anchorer):
        """An indexer derives ``base(ns) = keccak256(0x01 ‖ pad32(ns))`` itself: the count, then
        the peak of height ``h`` at ``base + 1 + h``.

        Read back through ``eth_getStorageAt``, against the node's storage rather than the code.
        Three leaves: the count is 3, the peaks at heights 1 and 0.
        """
        await append_all(w3, chain_id, anchorer, 3)

        base = int.from_bytes(keccak(b"\x01" + bytes(12) + bytes.fromhex(anchorer.address[2:])), "big")
        assert int.from_bytes(bytes(await w3.eth.get_storage_at(ADDR, base)), "big") == 3, "the count"
        assert bytes(await w3.eth.get_storage_at(ADDR, base + 1)) == hash_leaf(c(3)), "the peak of height 0"
        assert bytes(await w3.eth.get_storage_at(ADDR, base + 2)) == hash_merge(hash_leaf(c(1)), hash_leaf(c(2)))
        assert await root(w3, anchorer.address) == ROOTS[2]


class TestAppend:
    async def test_append_returns_the_root_and_emits(self, w3, chain_id, anchorer):
        commitment, metadata = b"\x11" * 32, b'{"v":1,"kind":"content"}'

        receipt = await append(w3, chain_id, anchorer, commitment, metadata)

        assert await root(w3, anchorer.address) == hash_leaf(commitment), "one leaf is its own root"
        (event,) = leaves_of(receipt)
        assert event.namespace.lower() == anchorer.address.lower()
        assert (event.index, event.commitment, event.metadata) == (0, commitment, metadata)
        assert event.root == hash_leaf(commitment)
        assert event.peaks == [hash_leaf(commitment)], "what a proof is checked against"
        assert await state(w3, anchorer.address) == (1, event.peaks)

    async def test_sequential_appends_reach_the_reference_roots(self, w3, chain_id, anchorer):
        """The same sixteen roots the precompile's unit tests and the contracts' suite pin.

        Read after each append rather than at the end, or the state would be the sixteenth's
        sixteen times over.
        """
        for i in range(1, 17):
            (event,) = leaves_of(await append(w3, chain_id, anchorer, c(i)))
            assert event.root == ROOTS[i - 1], f"root after leaf {i}"

            count, peaks = await state(w3, anchorer.address)
            assert (count, event.index) == (i, i - 1)
            assert len(peaks) == bin(i).count("1"), "one peak per set bit of the count"
            assert peaks == event.peaks, "the event carries what the chain holds"
            assert bag(peaks) == ROOTS[i - 1] == await root(w3, anchorer.address)

    async def test_the_same_commitment_may_be_appended_again(self, w3, chain_id, anchorer):
        """No no-op rule: a leaf is a position, so the same commitment twice is two leaves."""
        commitment = b"\x22" * 32
        first = await append(w3, chain_id, anchorer, commitment)
        second = await append(w3, chain_id, anchorer, commitment)

        assert [e.index for e in leaves_of(first) + leaves_of(second)] == [0, 1]
        assert (await state(w3, anchorer.address))[0] == 2
        assert await root(w3, anchorer.address) == hash_merge(hash_leaf(commitment), hash_leaf(commitment))

    async def test_a_batch_from_empty_reaches_the_sequential_root(self, w3, chain_id, anchorer):
        """Thirteen leaves cut aligned from zero -- sizes 8, 4, 1 -- reach the root thirteen
        appends reach. How a corpus loads."""
        chunks = [chunk(1, 8), chunk(9, 4), chunk(13, 1)]
        receipt = await append_leaves(w3, chain_id, anchorer, chunks, b"provenance")

        assert await root(w3, anchorer.address) == ROOTS[12]
        (batch,) = batches_of(receipt)
        assert (batch.first, batch.count) == (0, 13)
        assert batch.chunk_roots == [r for r, _ in chunks] and batch.chunk_heights == [3, 2, 0]
        assert (batch.root, batch.metadata) == (ROOTS[12], b"provenance")
        assert await state(w3, anchorer.address) == (13, batch.peaks)

    async def test_a_batch_after_a_prefix_is_cut_to_the_alignment(self, w3, chain_id, anchorer):
        """Five leaves one by one, then eight more: sizes rise to the boundary and fall after it."""
        await append_all(w3, chain_id, anchorer, 5)
        await append_leaves(w3, chain_id, anchorer, [chunk(6, 1), chunk(7, 2), chunk(9, 4), chunk(13, 1)])
        assert await root(w3, anchorer.address) == ROOTS[12]

    async def test_a_chunk_off_the_alignment_is_refused(self, w3, chain_id, anchorer):
        """A pair at count 5: a subtree's position has to be a multiple of its size."""
        await append_all(w3, chain_id, anchorer, 5)
        before = await root(w3, anchorer.address)

        pair, height = chunk(6, 2)
        data = A.fns.appendLeaves([pair], [height], b"").data
        assert CHUNK_NOT_ALIGNED in await call_revert(w3, ADDR, data, sender=anchorer.address)

        data = A.fns.appendLeaves([pair], [], b"").data
        assert CHUNKS_MISMATCH in await call_revert(w3, ADDR, data, sender=anchorer.address)

        assert await root(w3, anchorer.address) == before


class TestNamespaces:
    async def test_namespaces_are_isolated(self, w3, chain_id, anchorer):
        """Writes land under msg.sender, so one account cannot reach another's MMR."""
        other = new_account()
        await fund(w3, other.address)

        await append(w3, chain_id, anchorer, c(1))
        await append(w3, chain_id, other, c(1))
        assert await root(w3, anchorer.address) == await root(w3, other.address) == ROOTS[0]

        await append(w3, chain_id, anchorer, c(2))
        assert await root(w3, anchorer.address) == ROOTS[1]
        assert await root(w3, other.address) == ROOTS[0], "the other's MMR did not move"

    async def test_a_contract_appends_under_its_own_address(self, w3, chain_id, anchorer):
        """The namespace is msg.sender, not tx.origin -- which is what makes a wrapper contract
        a partition rather than a way into its caller's."""
        commitment = b"\xab" * 32
        _, forwarder = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=call_forwarder(ADDR)
        )

        receipt = await append(w3, chain_id, anchorer, commitment, to=forwarder)

        assert await root(w3, forwarder) == hash_leaf(commitment)
        assert await root(w3, anchorer.address) == ZERO32, "the sending EOA's namespace is untouched"
        (event,) = leaves_of(receipt)
        assert event.namespace.lower() == forwarder.lower(), "the log names the contract, not the origin"

    async def test_a_delegated_eoa_appends_under_its_own_address(self, w3, chain_id, anchorer):
        """EIP-7702: the code is borrowed, the namespace is not.

        Every account delegating to one smart-account implementation shares its code, so a
        namespace taken from there would put all of them in one partition.
        """
        commitment = b"\xcd" * 32
        _, delegate = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=call_forwarder(ADDR)
        )
        authority = new_account()  # fresh EOA: no code of its own, and no leaves

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
            data=bytes(A.fns.appendLeaf(commitment, b"").data),
            gas=SET_CODE_GAS + 2 * SSTORE_CREATE_COST,  # a first leaf creates the count and a peak
        )

        assert receipt["status"] == 1
        assert await root(w3, authority.address) == hash_leaf(commitment)
        assert await root(w3, delegate) == ZERO32, "not the implementation it borrowed"
        assert await root(w3, anchorer.address) == ZERO32, "nor the sponsor's"

    async def test_a_constructor_appends_under_the_address_being_created(self, w3, chain_id, anchorer):
        """The x/anchoring module refused a constructor call outright; here it is a caller like
        any other, under the address being created -- which holds no code yet, and never
        will."""
        commitment = b"\xbc" * 32
        initcode = _constructor_call(ADDR, bytes(A.fns.appendLeaf(commitment, b"").data))

        _, created = await deploy_contract(w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=initcode)

        assert bytes(await w3.eth.get_code(created)) == b"", "the constructor returns no runtime"
        assert await root(w3, created) == hash_leaf(commitment)
        assert await root(w3, anchorer.address) == ZERO32, "not the deployer's namespace"


class TestRefusedCalls:
    async def test_value_bearing_append_is_rejected(self, w3, chain_id, anchorer):
        """Refused by ``eth_call``, and refused again at admission as a real tx.

        There is no native token here, so a value-bearing call never reaches a block: the
        balance the x/anchoring suite watched has nothing to say, and the MMR is the whole
        record of what the attempt left behind.
        """
        data = A.fns.appendLeaf(b"\xaa" * 32, b"").data
        tx = {
            "to": ADDR,
            "from": anchorer.address,
            "data": "0x" + bytes(data).hex(),
            "gas": hex(2_000_000),
            "value": "0x1",
        }

        resp = await w3.provider.make_request("eth_call", [tx, "latest"])
        assert resp.get("error") is not None, f"value-bearing append must fail, got {resp!r}"

        with pytest.raises(Exception, match="value transfer not allowed"):
            await send_calls(
                w3, chain_id=chain_id, private_key=anchorer.key.hex(), calls=[{"to": ADDR, "value": 1, "data": data}]
            )
        assert await root(w3, anchorer.address) == ZERO32

    async def test_writes_are_rejected_in_a_read_only_frame(self, w3, chain_id, anchorer):
        """Reads serve a STATICCALL; writes refuse it. ``eth_call`` cannot make a read-only
        frame, so a deployed probe does."""
        await append(w3, chain_id, anchorer, c(1))

        _, probe = await deploy_contract(
            w3, chain_id=chain_id, private_key=anchorer.key.hex(), bytecode=staticcall_probe(ADDR)
        )

        async def succeeds(data) -> bool:
            out = await w3.eth.call({"to": probe, "data": data})
            return int.from_bytes(bytes(out), "big") == 1

        assert await succeeds(A.fns.root(anchorer.address).data), "root must serve a staticcall"
        assert await succeeds(A.fns.state(anchorer.address).data), "state must serve a staticcall"
        assert not await succeeds(A.fns.appendLeaf(c(2), b"").data), "appendLeaf must refuse one"
        # One aligned chunk: an empty batch is refused in any frame, so it would prove nothing.
        assert not await succeeds(A.fns.appendLeaves([hash_leaf(c(2))], [0], b"").data), "appendLeaves must refuse one"

        assert await root(w3, anchorer.address) == ROOTS[0], "the MMR is untouched"

    async def test_malformed_calldata_reverts(self, w3, anchorer):
        """A known selector with truncated arguments fails the ABI decode, before any state."""
        truncated = "0x" + bytes(A.fns.appendLeaf(b"\x11" * 32, b"").data)[:20].hex()

        await call_revert(w3, ADDR, truncated, sender=anchorer.address)
        assert await root(w3, anchorer.address) == ZERO32

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
    """What an append costs: an estimate that covers the receipt, the storage a new height
    creates, and a payload that is paid for. The x/anchoring suite watched all three."""

    async def test_estimate_covers_the_append_it_estimated(self, w3, chain_id, anchorer):
        """The appends that create a slot and the ones that only overwrite differ by an order
        of magnitude, so an estimate covering only the cheap one would be an out-of-gas tx."""
        for i in range(1, 5):
            data = A.fns.appendLeaf(c(i), b"").data
            estimate = await w3.eth.estimate_gas({"from": anchorer.address, "to": ADDR, "data": data})

            receipt = await append(w3, chain_id, anchorer, c(i))

            assert receipt["gasUsed"] <= estimate, f"estimate {estimate} short of {receipt['gasUsed']}"

    async def test_a_new_height_pays_for_its_slot_and_a_stale_one_is_reused(self, w3, chain_id, anchorer):
        """TIP-1000: a peak is metered storage, so the first leaf at a height creates its slot
        (~250k) -- and a peak that merged away is left in place, so the next leaf at that
        height overwrites rather than creates. The third leaf reuses height 0, the fourth
        opens height 2, the fifth reuses height 0 again."""
        await fund(w3, anchorer.address)  # account creation is paid; not what is measured
        receipts = await append_all(w3, chain_id, anchorer, 5)
        gas = [r["gasUsed"] for r in receipts]

        assert gas[1] - gas[2] >= SSTORE_CREATE_COST * 0.94, "the second leaf opened height 1; the third opened nothing"
        assert gas[3] - gas[2] >= SSTORE_CREATE_COST * 0.94, "the fourth opened height 2"
        assert abs(gas[4] - gas[2]) < SSTORE_CREATE_COST * 0.1, "the fifth, like the third, overwrote height 0"

    async def test_the_metadata_is_charged_for(self, w3, chain_id, anchorer):
        """The payload is only logged, never stored -- but still paid for, at no less than its
        own calldata costs. Otherwise the log would be a free channel."""
        extra = 1024
        await append_all(w3, chain_id, anchorer, 4)  # heights 0..2 exist; the next two reuse height 0

        small = await append(w3, chain_id, anchorer, c(5), b"x" * 32)
        await append(w3, chain_id, anchorer, c(6))  # back to a count whose next leaf reuses height 0
        big = await append(w3, chain_id, anchorer, c(7), b"x" * (32 + extra))

        delta = big["gasUsed"] - small["gasUsed"]
        assert delta >= 16 * extra, f"{extra} more bytes cost {delta}, under the calldata floor"


def leaves_sql(up_to: int) -> str:
    """Mirrors ``leaves_sql`` in nvnmchain-anchoring's ``src/tidx.rs``: every ``LeafAppended``
    row in log order, over the base ``logs`` table for the reason the second test pins, and
    bounded so a leaf cannot come from a block the state read did not cover."""
    return (
        "SELECT topic1 AS namespace, topic2 AS index, data, block_num, log_idx"
        f" FROM logs WHERE address = {bytea(ADDR)} AND selector = {bytea(LEAF_APPENDED.topic)}"
        f" AND block_num <= {up_to} ORDER BY block_num, log_idx"
    )


class TestThroughAnIndexer:
    """Payloads are left to the log "for indexers to derive", so the design holds only if one
    can fold the leaves back into the root the chain holds. These run the query an indexer
    uses, where every way of being wrong returns rows that parse. Needs ``--tidx``."""

    async def test_the_leaves_query_folds_to_the_precompiles_root(self, w3, chain_id, anchorer, tidx):
        """Every leaf the query returns, folded in order, is the MMR the chain kept --
        payloads and all."""
        commitments = [b"\x11" * 32, b"\x22" * 32, b"\x33" * 32]
        payloads = [b'{"v":1}', b"solo", b'{"v":2}']
        for commitment, payload in zip(commitments, payloads, strict=True):
            receipt = await append(w3, chain_id, anchorer, commitment, payload)
        at = tidx.bounded(receipt)

        ours = [
            (int.from_bytes(bytes(HexBytes(row["index"])), "big"), decode_leaf(row["data"]))
            for row in tidx.sql(leaves_sql(at))
            if HexBytes(row["namespace"])[-20:] == HexBytes(anchorer.address)
        ]
        assert [index for index, _ in ours] == [0, 1, 2], f"got {ours}"
        assert [leaf[3] for _, leaf in ours] == payloads, "the newest leaf's payload and all the others'"

        mmr = Mmr()
        for _, (commitment, _, peaks, _) in ours:
            mmr.append(commitment)
            assert mmr.peaks == peaks, "the event carries the peaks the fold reaches"
        assert mmr.root == await root(w3, anchorer.address), "the fold is the chain's root"
        assert await state(w3, anchorer.address) == (3, mmr.peaks)

    async def test_metadata_does_not_survive_tidx_decoding(self, w3, chain_id, anchorer, tidx):
        """Why the query above reads raw ``data`` instead of a decoded event table.

        tidx decodes a dynamic argument as the 32-byte head word -- the ABI offset, not the
        payload -- while a fixed-width one gets a real dereference. An indexer trusting that
        column would hash an offset for every leaf. Pinned rather than worked around
        silently: if tidx grows the dereference this fails, and the raw read can go.
        """
        commitment, metadata = b"\x33" * 32, b"payload-not-offset"
        receipt = await append(w3, chain_id, anchorer, commitment, metadata)
        (event,) = leaves_of(receipt)

        (row,) = tidx.sql(
            f"SELECT commitment, metadata FROM LeafAppended WHERE namespace = {bytea(anchorer.address)}"
            f" AND block_num <= {tidx.bounded(receipt)}",
            signature=LEAF_APPENDED_EVENT,
        )

        assert HexBytes(row["commitment"]) == commitment, "a fixed-width argument decodes correctly"
        assert HexBytes(row["metadata"]) != metadata, "if this now holds, tidx decodes dynamic bytes"
        # The metadata's offset: past the four head words, the peaks' length and the peaks.
        offset = 4 * 32 + 32 + 32 * len(event.peaks)
        assert int.from_bytes(bytes(HexBytes(row["metadata"])), "big") == offset, "it is the ABI offset word"
