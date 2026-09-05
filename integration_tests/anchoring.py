"""Reading the anchoring precompile: its two events, its state, and the hashing it uses."""

from collections import namedtuple

from eth_abi.abi import decode
from eth_utils import keccak
from hexbytes import HexBytes

from .abi import ANCHORING, ANCHORING_ADDRESS
from .tidx import named_signature

LEAF_APPENDED = ANCHORING.events.LeafAppended
LEAVES_APPENDED = ANCHORING.events.LeavesAppended
LEAF_APPENDED_TOPIC = HexBytes(LEAF_APPENDED.topic)
LEAVES_APPENDED_TOPIC = HexBytes(LEAVES_APPENDED.topic)

# The events as tidx's ``signature=`` takes them, and the non-indexed argument types in
# order -- which is what a log's ``data`` column holds.
LEAF_APPENDED_EVENT = named_signature(LEAF_APPENDED)
LEAF_TYPES = [a["type"] for a in LEAF_APPENDED.abi.get("inputs", []) if not a.get("indexed")]
LEAVES_TYPES = [a["type"] for a in LEAVES_APPENDED.abi.get("inputs", []) if not a.get("indexed")]

#: One ``LeafAppended`` log, decoded.
Leaf = namedtuple("Leaf", "namespace index commitment root peaks metadata")
#: One ``LeavesAppended`` log, decoded.
Leaves = namedtuple("Leaves", "namespace first count chunk_roots chunk_heights root peaks metadata")


def hash_leaf(commitment: bytes) -> bytes:
    """``keccak256("leaf" ‖ commitment)`` -- what a commitment is as a node of the MMR."""
    return keccak(b"leaf" + commitment)


def hash_merge(left: bytes, right: bytes) -> bytes:
    return keccak(b"merge" + left + right)


def bag(peaks: list[bytes]) -> bytes:
    """The root: the peaks bagged highest first, zero when there are none."""
    if not peaks:
        return b"\x00" * 32
    out = peaks[0]
    for peak in peaks[1:]:
        out = keccak(b"bag" + out + peak)
    return out


def decode_leaf(data) -> tuple[bytes, bytes, list[bytes], bytes]:
    """A ``LeafAppended`` log's ``data`` as ``(commitment, root, peaks, metadata)``.

    Read here rather than from a decoded event table: tidx returns a dynamic argument as its
    ABI offset word, which ``test_anchoring.py`` pins.
    """
    commitment, root, peaks, metadata = decode(LEAF_TYPES, bytes(HexBytes(data)))
    return commitment, root, list(peaks), metadata


def _namespace(topic) -> str:
    return "0x" + bytes(topic)[-20:].hex()


def leaves_of(receipt) -> list[Leaf]:
    """The receipt's ``LeafAppended`` events, decoded, in canonical log order."""
    return [
        Leaf(_namespace(lg["topics"][1]), int.from_bytes(bytes(lg["topics"][2]), "big"), *decode_leaf(lg["data"]))
        for lg in receipt["logs"]
        if lg["address"].lower() == ANCHORING_ADDRESS.lower() and HexBytes(lg["topics"][0]) == LEAF_APPENDED_TOPIC
    ]


def batches_of(receipt) -> list[Leaves]:
    """The receipt's ``LeavesAppended`` events, decoded, in canonical log order."""
    out = []
    for lg in receipt["logs"]:
        if lg["address"].lower() != ANCHORING_ADDRESS.lower() or HexBytes(lg["topics"][0]) != LEAVES_APPENDED_TOPIC:
            continue
        count, chunk_roots, chunk_heights, root, peaks, metadata = decode(LEAVES_TYPES, bytes(HexBytes(lg["data"])))
        first = int.from_bytes(bytes(lg["topics"][2]), "big")
        out.append(
            Leaves(
                _namespace(lg["topics"][1]),
                first,
                count,
                list(chunk_roots),
                list(chunk_heights),
                root,
                list(peaks),
                metadata,
            )
        )
    return out


async def root(w3, namespace) -> bytes:
    """The root of ``namespace``'s MMR, zero if nothing was ever appended."""
    return bytes(await ANCHORING.fns.root(namespace).call(w3, to=ANCHORING_ADDRESS))


async def state(w3, namespace) -> tuple[int, list[bytes]]:
    """The leaf count and the peaks, highest first -- what a proof is checked against."""
    count, peaks = await ANCHORING.fns.state(namespace).call(w3, to=ANCHORING_ADDRESS)
    return count, [bytes(p) for p in peaks]


async def leaf_logs(w3, namespace, *, from_block=0):
    """Every ``LeafAppended`` log under ``namespace``, oldest first. The namespace is indexed,
    so it narrows at the node rather than in a comprehension."""
    topics = [LEAF_APPENDED_TOPIC, HexBytes(bytes(12) + HexBytes(namespace))]
    return await w3.eth.get_logs({"fromBlock": from_block, "address": ANCHORING_ADDRESS, "topics": topics})


async def append_logs(w3, namespace, *, from_block=0):
    """Every append under ``namespace``, of either shape, oldest first."""
    logs = []
    for topic0 in (LEAF_APPENDED_TOPIC, LEAVES_APPENDED_TOPIC):
        topics = [topic0, HexBytes(bytes(12) + HexBytes(namespace))]
        logs += await w3.eth.get_logs({"fromBlock": from_block, "address": ANCHORING_ADDRESS, "topics": topics})
    return sorted(logs, key=lambda lg: (lg["blockNumber"], lg["logIndex"]))


class Mmr:
    """The MMR as this suite folds it, to predict what the precompile will hold: leaves and
    chunks pushed in order, the peaks highest first."""

    def __init__(self):
        self.count, self.peaks = 0, []

    def push(self, height: int, node: bytes) -> None:
        size = 1 << height
        assert self.count % size == 0, f"a chunk of height {height} at count {self.count}"
        while (self.count >> height) & 1:
            node = hash_merge(self.peaks.pop(), node)
            height += 1
        self.peaks.append(node)
        self.count += size

    def append(self, commitment: bytes) -> None:
        self.push(0, hash_leaf(commitment))

    @property
    def root(self) -> bytes:
        return bag(self.peaks)


def perfect(commitments: list[bytes]) -> bytes:
    """The root of a perfect tree over ``commitments``, as a caller cuts a batch."""
    nodes = [hash_leaf(c) for c in commitments]
    while len(nodes) > 1:
        nodes = [hash_merge(nodes[i], nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0]
