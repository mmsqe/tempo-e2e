"""Two owners seeding a registry admin through the break-glass grant, on a node.

Safe covers the Safe's own rules; only a node shows the alloc leaving a Safe already set up, and
the gate letting the module admin past while it still refuses every other contract.

Pointed at another node with ``--tempo-rpc``, what is left is what a launch genesis can still get
wrong -- the Safe set up, owned by the three it should be. Its owners are the member keys, which
are not ours to sign with, so the cases that send anything skip.
"""

from __future__ import annotations

import pytest
from eth_utils import keccak

from .abi import ANCHORING, ANCHORING_ADDRESS, MODULE_ADMIN_ADDRESS, MODULE_ADMIN_SAFE
from .anchoring import anchoring_node, deploy_relay, emitted, new_registry
from .network import ExternalNode
from .utils import call_revert, fund, new_account, send_call

pytestmark = [pytest.mark.tempo, pytest.mark.anchoring]

THRESHOLD = 2
ZERO = "0x" + "00" * 20
SENTINEL = "0x" + "00" * 19 + "01"  # the head of Safe's owner list, which is circular


@pytest.fixture(scope="module")
def owners():
    """Three accounts standing in for the member keys, which a dev node cannot sign with."""
    return [new_account() for _ in range(3)]


@pytest.fixture(scope="module")
def attached(request):
    return bool(request.config.getoption("--tempo-rpc"))


@pytest.fixture
def signing_owners(owners, attached):
    """The owners this run can sign for: none when attached to someone else's node."""
    if attached:
        pytest.skip("the attached node's module admin is owned by keys we do not hold")
    return owners


@pytest.fixture(scope="module")
def tempo(request, tmp_path_factory, owners, attached):
    """A node whose module admin is a Safe owned by ``owners``, as the launch genesis places it --
    or the node ``--tempo-rpc`` names, whatever its genesis wrote."""
    if attached:
        rpc = request.config.getoption("--tempo-rpc")
        yield ExternalNode(rpc, request.config.getoption("--tempo-ws")).wait_for_rpc()
        return
    with anchoring_node(
        tmp_path_factory.mktemp("module-admin"),
        MODULE_ADMIN_ADDRESS,
        module_admin_owners=[o.address for o in owners],
    ) as node:
        yield node


async def safe_tx(w3, to: str, data, signers) -> bytes:
    """``data`` as the ``execTransaction`` call that runs it, signed for the Safe by ``signers``.

    No value and no refund: gasToken and refundReceiver go unused because gasPrice is 0, and the
    operation is a plain call. The owners sign the hash the Safe itself hands out, so nothing here
    rebuilds its 712 domain; Safe walks the blob expecting ascending signers, so they sort.
    """
    args = (to, 0, data, 0, 0, 0, 0, ZERO, ZERO)
    nonce = await MODULE_ADMIN_SAFE.fns.nonce().call(w3)
    digest = await MODULE_ADMIN_SAFE.fns.getTransactionHash(*args, nonce).call(w3)
    signed = sorted(signers, key=lambda owner: int(owner.address, 16))
    blob = b"".join(owner.unsafe_sign_hash(digest).signature for owner in signed)
    return MODULE_ADMIN_SAFE.fns.execTransaction(*args, blob).data


async def registry_by_a_stranger(w3, chain_id) -> tuple[int, bytes, str]:
    """A registry nobody in the Safe administers, the calldata to seed an admin on it, and the
    account it names."""
    stranger = new_account()
    await fund(w3, stranger.address)
    registry_id = await new_registry(w3, chain_id, stranger, "module-admin")
    seeded = new_account().address
    grant = ANCHORING.fns.grantRole(registry_id, "", seeded, "admin").data
    return registry_id, grant, seeded


def admin_role(registry_id: int) -> bytes:
    return keccak(text=f"registry:{registry_id}:admin")


class TestGenesis:
    """The alloc, which is all an attached node can be asked about."""

    async def test_the_alloc_placed_a_safe_already_set_up(self, w3, owners, attached):
        """Genesis has no transaction to run ``setup()`` with, so it writes what setup writes."""
        assert bytes(await w3.eth.get_code(MODULE_ADMIN_ADDRESS)), "nothing is at the module admin"
        held = [a.lower() for a in await MODULE_ADMIN_SAFE.fns.getOwners().call(w3)]
        assert await MODULE_ADMIN_SAFE.fns.getThreshold().call(w3) == THRESHOLD
        if not attached:
            assert held == [o.address.lower() for o in owners]
        assert len(set(held)) == 3, f"an owner is missing or repeated: {held}"

    async def test_setup_cannot_be_run_again(self, w3):
        """A Safe already set up is nobody else's to claim."""
        taken = MODULE_ADMIN_SAFE.fns.setup([new_account().address], 1, ZERO, b"", ZERO, ZERO, 0, ZERO).data
        assert "GS200" in await call_revert(w3, MODULE_ADMIN_ADDRESS, taken)


class TestBreakGlass:
    """Seeding a registry admin: two owners, reaching `Anchoring` past the gate."""

    async def test_two_owners_seed_a_registry_admin(self, w3, chain_id, signing_owners):
        first, second, _ = signing_owners
        relayer = new_account()
        await fund(w3, relayer.address)
        registry_id, grant, seeded = await registry_by_a_stranger(w3, chain_id)

        run = await safe_tx(w3, ANCHORING_ADDRESS, grant, [first, second])
        receipt = await send_call(w3, chain_id, relayer, MODULE_ADMIN_ADDRESS, run)

        assert await ANCHORING.fns.hasRole(admin_role(registry_id), seeded).call(w3), "the grant did not land"
        # The anchoring contract saw the Safe as the caller: that is the sender the gate let past.
        assert emitted(receipt, "GrantRole") == [(MODULE_ADMIN_ADDRESS, registry_id, "", seeded, "admin")]

    async def test_one_owner_is_not_a_threshold(self, w3, chain_id, signing_owners):
        _, _, third = signing_owners
        _, grant, _ = await registry_by_a_stranger(w3, chain_id)

        alone = await safe_tx(w3, ANCHORING_ADDRESS, grant, [third])
        assert "GS020" in await call_revert(w3, MODULE_ADMIN_ADDRESS, alone, sender=third.address)

    async def test_two_owners_replace_the_third(self, w3, chain_id, signing_owners):
        """What the old chain's `MsgUpdateParams` allowed: the admin naming a new one, by
        transaction. Run last, since it leaves the outgoing owner unable to sign."""
        first, second, third = signing_owners
        relayer = new_account()
        await fund(w3, relayer.address)
        held = [a.lower() for a in await MODULE_ADMIN_SAFE.fns.getOwners().call(w3)]
        # swapOwner names the owner pointing at the one replaced -- the sentinel, for the first.
        pointing_at = ([SENTINEL] + held)[held.index(third.address.lower())]

        incoming = new_account()
        swap = MODULE_ADMIN_SAFE.fns.swapOwner(pointing_at, third.address, incoming.address).data
        run = await safe_tx(w3, MODULE_ADMIN_ADDRESS, swap, [first, second])
        await send_call(w3, chain_id, relayer, MODULE_ADMIN_ADDRESS, run)

        assert await MODULE_ADMIN_SAFE.fns.isOwner(incoming.address).call(w3)
        assert not await MODULE_ADMIN_SAFE.fns.isOwner(third.address).call(w3)
        assert await MODULE_ADMIN_SAFE.fns.getThreshold().call(w3) == THRESHOLD

    async def test_the_gate_still_stands_for_any_other_contract(self, w3, chain_id, signing_owners):
        """The exemption names the module admin alone; any other contract is still refused."""
        first, _, _ = signing_owners
        await fund(w3, first.address)
        _, grant, _ = await registry_by_a_stranger(w3, chain_id)

        relay = await deploy_relay(w3, chain_id, first)
        assert "sender not an eoa" in await call_revert(w3, relay, grant, sender=first.address)
