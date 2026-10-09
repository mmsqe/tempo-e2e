"""Evidence that a consensus key signed conflicting votes, built by hand from the key: for a test
or a rehearsal, where no validator signs twice by itself.

    python -m integration_tests.evidence --key signing.key --epoch 3 --rpc-url http://127.0.0.1:9545
"""

import argparse
from pathlib import Path

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from web3 import Web3


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        value, low = value >> 7, value & 0x7F
        out.append(low | (0x80 if value else 0))
        if not value:
            return bytes(out)


def notarize(key, chain_id: int, genesis: bytes, epoch: int, payload: int) -> bytes:
    """A notarize vote for view 3 of `epoch`, as the node encodes it in evidence, under `key`'s
    signature for the chain of `chain_id` and `genesis`."""
    body = varint(epoch) + varint(3) + varint(2) + bytes([payload]) * 32
    namespace = b"TEMPO_ATTRIBUTABLE_" + chain_id.to_bytes(8, "big") + genesis + b"_NOTARIZE"
    signature = eddsa.new(key, "rfc8032").sign(varint(len(namespace)) + namespace + body)
    return b"\x00" + body + signature


def double_notarize(key, chain_id: int, genesis: bytes, epoch: int, payloads=(1, 2)) -> bytes:
    """`key` notarizing the proposals `payloads` name in one round of `epoch`."""
    signer = key.public_key().export_key(format="raw")
    return signer + b"".join(notarize(key, chain_id, genesis, epoch, payload) for payload in payloads)


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
