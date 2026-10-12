"""Evidence that a validator signed conflicting votes, built by hand from its signing key: for a
test or a rehearsal, where no validator signs twice by itself.

    python -m integration_tests.evidence --key signing.key --epoch 3 --rpc-url http://127.0.0.1:9545

Votes are signed with the validator's vote key, the BLS12-381 key the node derives from its signing
key. This derives it and signs apart from the node's code, so the tests that use it check the two
agree."""

import argparse
from hashlib import sha256
from pathlib import Path

from Crypto.PublicKey import ECC
from py_ecc.bls.hash import expand_message_xmd
from py_ecc.bls.hash_to_curve import hash_to_G1
from py_ecc.bls.point_compression import compress_G1, compress_G2
from py_ecc.optimized_bls12_381 import G2, curve_order, multiply
from web3 import Web3

# Signatures are in G1 and keys in G2, as the node's threshold scheme has them.
MESSAGE = b"BLS_SIG_BLS12381G1_XMD:SHA-256_SSWU_RO_POP_"
PROOF_OF_POSSESSION = b"BLS_POP_BLS12381G1_XMD:SHA-256_SSWU_RO_POP_"

# The kinds of ballot as the registry numbers them, and what each is signed under.
NOTARIZE, NULLIFY, FINALIZE = range(3)
NAMESPACES = (b"_NOTARIZE", b"_NULLIFY", b"_FINALIZE")


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        value, low = value >> 7, value & 0x7F
        out.append(low | (0x80 if value else 0))
        if not value:
            return bytes(out)


def vote_key(seed: bytes) -> int:
    """The vote key of the validator whose signing key has `seed`."""
    return int.from_bytes(expand_message_xmd(seed, b"TEMPO_VOTE_KEY", 48, sha256), "big") % curve_order


def public_key(secret: int) -> bytes:
    """A vote key's public key, as the registry holds it."""
    high, low = compress_G2(multiply(G2, secret))
    return high.to_bytes(48, "big") + low.to_bytes(48, "big")


def sign(secret: int, dst: bytes, namespace: bytes, message: bytes) -> bytes:
    """The vote key's signature as the node's library makes it: over the length-prefixed namespace,
    then the message."""
    point = hash_to_G1(varint(len(namespace)) + namespace + message, dst, sha256)
    return compress_G1(multiply(point, secret)).to_bytes(48, "big")


def registration(key, chain_id: int) -> tuple[bytes, bytes]:
    """What `setVoteKey` takes for the validator whose signing key is `key`: its vote key, and
    that key's proof of possession for this validator on the chain of `chain_id`."""
    secret = vote_key(key.seed)
    public = public_key(secret)
    validator = key.public_key().export_key(format="raw")
    namespace = b"TEMPO_VALIDATOR_CONFIG_V2_VOTE_KEY" + chain_id.to_bytes(8, "big") + validator
    return public, sign(secret, PROOF_OF_POSSESSION, namespace, public)


def ballot(key, chain_id: int, genesis: bytes, epoch: int, kind: int, payload: int = 0, view: int = 3) -> bytes:
    """A vote of `kind` for `view` of `epoch`, as the node encodes it in evidence, under the
    signature of `key`'s vote key for the chain of `chain_id` and `genesis`. A nullify names no
    proposal, so it takes no `payload`."""
    body = varint(epoch) + varint(view)
    if kind != NULLIFY:
        body += varint(view - 1) + bytes([payload]) * 32
    namespace = b"TEMPO_ATTRIBUTABLE_" + chain_id.to_bytes(8, "big") + genesis + NAMESPACES[kind]
    return bytes([kind]) + body + sign(vote_key(key.seed), MESSAGE, namespace, body)


def against(key, *ballots: bytes) -> bytes:
    """Evidence holding two ballots against the validator `key`."""
    return key.public_key().export_key(format="raw") + b"".join(ballots)


def double_notarize(key, chain_id: int, genesis: bytes, epoch: int, payloads=(1, 2), view: int = 3) -> bytes:
    """The validator `key` notarizing the proposals `payloads` name in the round `view` of `epoch`."""
    return against(key, *(ballot(key, chain_id, genesis, epoch, NOTARIZE, payload, view) for payload in payloads))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--key", type=Path, required=True, help="a node's signing.key: 0x and the seed in hex")
    parser.add_argument("--epoch", type=int, required=True, help="the epoch the two votes are for")
    parser.add_argument("--rpc-url", required=True, help="a node of the chain the votes are signed for")
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc_url))
    # Raw, because a consensus chain's genesis extraData is past what web3.py's formatter takes.
    genesis = w3.provider.make_request("eth_getBlockByNumber", ["0x0", False])["result"]["hash"]
    key = ECC.construct(curve="ed25519", seed=bytes.fromhex(args.key.read_text().strip().removeprefix("0x")))
    print("0x" + double_notarize(key, w3.eth.chain_id, bytes.fromhex(genesis[2:]), args.epoch).hex())


if __name__ == "__main__":
    main()
