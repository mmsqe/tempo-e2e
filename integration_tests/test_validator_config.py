"""TIP-1017 ValidatorConfig V2 (0xCccC…01): the validator set after genesis.

The set genesis writes is not the set the chain keeps — the owner may add, and an owner or the
validator itself may deactivate. The dev genesis seeds the registry initialized at height 0 with
zero validators, owned by the prefunded dev account, so both halves are assertable here. What a
single dev node cannot show is the other half of a join: the DKG re-ceremony that hands the new
validator a signing share at the next epoch, which needs a chain that runs consensus.
"""

import pytest
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from tempo.constants import VALIDATOR_CONFIG_V2_ADDRESS as V2_ADDR

from .abi import VALIDATOR_CONFIG_V2 as V2
from .network import FAUCET_PRIVATE_KEY
from .utils import call_revert, new_account, send_call

pytestmark = pytest.mark.tempo

NS_ADD = b"TEMPO_VALIDATOR_CONFIG_V2_ADD_VALIDATOR"


def enrolment(chain_id: int, address: str, ingress: str, egress: str | None = None, fee: str | None = None):
    """An Ed25519 key and the signature `addValidator` demands over its own terms.

    The digest binds the chain, this contract, the address, both endpoints and the fee recipient,
    so an enrolment cannot be lifted to another chain or another address. The namespace is
    prepended as `len ‖ namespace` rather than hashed in, which is what keeps a signature made for
    a rotation from passing as one made for a join.

    `ingress` is `ip:port` and `egress` a bare `ip` — the endpoints are validated before the
    signature, so neither may be empty, and by default a node dials out from where it listens.
    """
    key = ECC.generate(curve="ed25519")
    egress = ingress.rsplit(":", 1)[0] if egress is None else egress
    fee = fee or address
    digest = keccak(
        chain_id.to_bytes(8, "big")
        + bytes.fromhex(V2_ADDR[2:])
        + bytes.fromhex(address[2:])
        + bytes([len(ingress)])
        + ingress.encode()
        + bytes([len(egress)])
        + egress.encode()
        + bytes.fromhex(fee[2:])
    )
    signature = eddsa.new(key, "rfc8032").sign(bytes([len(NS_ADD)]) + NS_ADD + digest)
    return key.public_key().export_key(format="raw"), signature, egress, fee


async def join(w3, chain_id, owner, address: str, ingress: str):
    """The owner enrolling a validator that signed for it, which is the only way in."""
    pubkey, signature, egress, fee = enrolment(chain_id, address, ingress)
    add = V2.fns.addValidator(address, pubkey, ingress, egress, fee, signature)
    return pubkey, await send_call(w3, chain_id, owner, V2_ADDR, add.data)


@pytest.fixture
def owner():
    return Account.from_key(FAUCET_PRIVATE_KEY)


class TestGenesisState:
    async def test_the_dev_genesis_seeds_an_empty_registry(self, w3, owner):
        """Read at block 0, so a validator another test adds cannot make this pass or fail."""
        assert await V2.fns.isInitialized().call(w3, to=V2_ADDR, block_identifier=0)
        assert await V2.fns.getInitializedAtHeight().call(w3, to=V2_ADDR, block_identifier=0) == 0
        assert await V2.fns.validatorCount().call(w3, to=V2_ADDR, block_identifier=0) == 0
        assert not await V2.fns.getActiveValidators().call(w3, to=V2_ADDR, block_identifier=0)
        assert await V2.fns.getNextNetworkIdentityRotationEpoch().call(w3, to=V2_ADDR, block_identifier=0) == 0
        held = await V2.fns.owner().call(w3, to=V2_ADDR, block_identifier=0)
        assert to_checksum_address(held) == owner.address  # the prefunded dev account


class TestJoining:
    """The set grows after block 0, which is the whole point of the registry being on chain."""

    async def test_the_owner_adds_a_validator(self, w3, chain_id, owner):
        before = await V2.fns.validatorCount().call(w3, to=V2_ADDR)
        joiner = new_account()
        ingress = f"10.0.0.{1 + before % 250}:26656"
        pubkey, receipt = await join(w3, chain_id, owner, joiner.address, ingress)
        assert receipt["status"] == 1

        assert await V2.fns.validatorCount().call(w3, to=V2_ADDR) == before + 1
        entry = await V2.fns.validatorByAddress(joiner.address).call(w3, to=V2_ADDR)
        assert entry[0] == pubkey and to_checksum_address(entry[1]) == joiner.address
        assert entry[2] == ingress
        assert entry[7] == 0, "a fresh validator is active, not deactivated"
        assert entry == await V2.fns.validatorByPublicKey(pubkey).call(w3, to=V2_ADDR)
        active = await V2.fns.getActiveValidators().call(w3, to=V2_ADDR)
        assert joiner.address in [to_checksum_address(v[1]) for v in active]

    async def test_a_validator_deactivates_itself(self, w3, chain_id, owner, funded_account):
        """Deactivation is not owner-only: an operator can withdraw its own node."""
        joiner = funded_account
        ingress = "10.9.0.1:26656"
        await join(w3, chain_id, owner, joiner.address, ingress)
        index = (await V2.fns.validatorByAddress(joiner.address).call(w3, to=V2_ADDR))[5]

        receipt = await send_call(w3, chain_id, joiner, V2_ADDR, V2.fns.deactivateValidator(index).data)
        assert receipt["status"] == 1
        assert (await V2.fns.validatorByAddress(joiner.address).call(w3, to=V2_ADDR))[7] != 0
        assert joiner.address not in [
            to_checksum_address(v[1]) for v in await V2.fns.getActiveValidators().call(w3, to=V2_ADDR)
        ]


class TestRefusals:
    """What the registry will not accept, which is what makes the owner's power bounded."""

    async def test_mutators_are_owner_gated(self, w3):
        outsider = new_account()
        add = V2.fns.addValidator(outsider.address, b"\x11" * 32, "1.2.3.4:26656", "", outsider.address, b"")
        assert "Unauthorized" in await call_revert(w3, V2_ADDR, add.data, sender=outsider.address)
        handoff = V2.fns.transferOwnership(outsider.address)
        assert "Unauthorized" in await call_revert(w3, V2_ADDR, handoff.data, sender=outsider.address)

    async def test_the_owner_cannot_enrol_a_key_it_does_not_hold(self, w3, chain_id, owner):
        """The signature is what stops an admin from naming a node nobody runs."""
        joiner = new_account()
        pubkey, signature, egress, fee = enrolment(chain_id, joiner.address, "10.8.0.1:26656")
        stolen = V2.fns.addValidator(joiner.address, pubkey, "10.8.0.2:26656", egress, fee, signature)
        assert "InvalidSignature" in await call_revert(w3, V2_ADDR, stolen.data, sender=owner.address)

    async def test_a_signature_does_not_cross_chains(self, w3, chain_id, owner):
        joiner = new_account()
        pubkey, signature, egress, fee = enrolment(chain_id + 1, joiner.address, "10.7.0.1:26656")
        add = V2.fns.addValidator(joiner.address, pubkey, "10.7.0.1:26656", egress, fee, signature)
        assert "InvalidSignature" in await call_revert(w3, V2_ADDR, add.data, sender=owner.address)

    async def test_an_endpoint_has_to_be_one(self, w3, chain_id, owner):
        joiner = new_account()
        pubkey, signature, egress, fee = enrolment(chain_id, joiner.address, "not-an-endpoint", egress="10.6.0.1")
        add = V2.fns.addValidator(joiner.address, pubkey, "not-an-endpoint", egress, fee, signature)
        assert "NotIpPort" in await call_revert(w3, V2_ADDR, add.data, sender=owner.address)
