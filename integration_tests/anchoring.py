from eth_abi.abi import decode
from hexbytes import HexBytes

from .abi import ANCHORING, ANCHORING_ADDRESS
from .tidx import named_signature

ANCHORED = ANCHORING.events.Anchored
ANCHORED_TOPIC = HexBytes(ANCHORED.topic)

# The event as tidx's ``signature=`` takes it, and the non-indexed argument types in order
# -- which is what the log's ``data`` column holds.
ANCHORED_EVENT = named_signature(ANCHORED)
PAYLOAD_TYPES = [a["type"] for a in ANCHORED.abi.get("inputs", []) if not a.get("indexed")]


def decode_payload(data) -> tuple[bytes, bytes]:
    """An ``Anchored`` log's ``data`` as ``(commitment, metadata)``.

    Read here rather than from a decoded event table: tidx returns a dynamic ``bytes``
    argument as its ABI offset word, which ``test_anchoring.py`` pins.
    """
    return decode(PAYLOAD_TYPES, bytes(HexBytes(data)))


async def latest(w3, namespace, key) -> bytes:
    """The precompile's current commitment for ``(namespace, key)``, zero if never anchored."""
    return bytes(await ANCHORING.fns.latest(namespace, key).call(w3, to=ANCHORING_ADDRESS))


async def anchored_logs(w3, namespace, *, key=None, from_block=0):
    """Every ``Anchored`` log under ``namespace``, newest last, optionally one key only.

    Both are indexed arguments, so both narrow at the node rather than in a comprehension.
    """
    topics = [ANCHORED_TOPIC, HexBytes(bytes(12) + HexBytes(namespace))]
    if key is not None:
        topics.append(HexBytes(key))
    return await w3.eth.get_logs({"fromBlock": from_block, "address": ANCHORING_ADDRESS, "topics": topics})
