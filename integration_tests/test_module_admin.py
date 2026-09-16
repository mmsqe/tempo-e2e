"""Two owners seeding a registry admin through the break-glass grant, on a node.

Forge covers the multisig's rules; only a node shows the alloc placing both contracts and the
gate letting the module admin past while it still refuses every other contract.

Pointed at another node with ``--tempo-rpc``, what is left is what a launch genesis can still get
wrong -- the code placed, and the owners in the right slots. Its owners are the member keys, which
are not ours to sign with, so the cases that send anything skip.
"""

from __future__ import annotations

import pytest
from eth_utils import keccak

from .abi import ANCHORING, MODULE_ADMIN_ADDRESS, MODULE_ADMIN_MULTISIG
from .anchoring import MULTISIG_RUNTIME, anchoring_node, deploy_relay, emitted, new_registry
from .network import ExternalNode
from .utils import call_revert, fund, new_account, send_call

pytestmark = [pytest.mark.tempo, pytest.mark.anchoring]

THRESHOLD = 2


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
    """A node whose module admin is the multisig, ``owners`` in its slots, as the launch genesis
    places it -- or the node ``--tempo-rpc`` names, whatever its genesis wrote."""
    if attached:
        rpc = request.config.getoption("--tempo-rpc")
        yield ExternalNode(rpc, request.config.getoption("--tempo-ws")).wait_for_rpc()
        return
    with anchoring_node(
        tmp_path_factory.mktemp("module-admin"), MODULE_ADMIN_ADDRESS, multisig_owners=[o.address for o in owners]
    ) as node:
        yield node


async def registry_by_a_stranger(w3, chain_id) -> tuple[int, bytes, str]:
    """A registry nobody in the multisig administers, the calldata to seed an admin on it, and
    the account it names."""
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

    async def test_the_alloc_placed_the_multisig(self, w3, owners, attached):
        """What a launch genesis can get wrong: the code, and the owners in slots 0..2."""
        assert bytes(await w3.eth.get_code(MODULE_ADMIN_ADDRESS)) == MULTISIG_RUNTIME
        held, threshold = await MODULE_ADMIN_MULTISIG.fns.owners().call(w3)
        held = [a.lower() for a in held]  # the contract answers lowercase, an account is checksummed
        assert threshold == THRESHOLD
        if not attached:
            assert held == [o.address.lower() for o in owners]
        assert len(set(held)) == 3, f"an owner slot is empty or repeated: {held}"


class TestBreakGlass:
    """Seeding a registry admin: two owners, reaching `Anchoring` past the gate."""

    async def test_two_owners_seed_a_registry_admin(self, w3, chain_id, signing_owners):
        first, second, _ = signing_owners
        for owner in (first, second):
            await fund(w3, owner.address)
        registry_id, grant, seeded = await registry_by_a_stranger(w3, chain_id)

        await send_call(w3, chain_id, first, MODULE_ADMIN_ADDRESS, MODULE_ADMIN_MULTISIG.fns.propose(grant).data)
        proposal_id = await MODULE_ADMIN_MULTISIG.fns.proposalCount().call(w3)
        assert not await ANCHORING.fns.hasRole(admin_role(registry_id), seeded).call(w3), "one owner granted it"

        confirm = MODULE_ADMIN_MULTISIG.fns.confirm(proposal_id).data
        receipt = await send_call(w3, chain_id, second, MODULE_ADMIN_ADDRESS, confirm)

        assert await ANCHORING.fns.hasRole(admin_role(registry_id), seeded).call(w3), "the grant did not land"
        # The anchoring contract saw the multisig as the caller: that is the sender the gate let past.
        assert emitted(receipt, "GrantRole") == [(MODULE_ADMIN_ADDRESS, registry_id, "", seeded, "admin")]
        _, confirmations, executed = await MODULE_ADMIN_MULTISIG.fns.proposal(proposal_id).call(w3)
        assert (confirmations, executed) == (2, True)

    async def test_one_owner_is_not_a_threshold(self, w3, chain_id, signing_owners):
        _, _, third = signing_owners
        await fund(w3, third.address)
        registry_id, grant, seeded = await registry_by_a_stranger(w3, chain_id)

        await send_call(w3, chain_id, third, MODULE_ADMIN_ADDRESS, MODULE_ADMIN_MULTISIG.fns.propose(grant).data)
        proposal_id = await MODULE_ADMIN_MULTISIG.fns.proposalCount().call(w3)

        _, confirmations, executed = await MODULE_ADMIN_MULTISIG.fns.proposal(proposal_id).call(w3)
        assert (confirmations, executed) == (1, False)
        assert not await ANCHORING.fns.hasRole(admin_role(registry_id), seeded).call(w3)

    async def test_the_gate_still_stands_for_any_other_contract(self, w3, chain_id, signing_owners):
        """The exemption names the module admin alone; any other contract is still refused."""
        first, _, _ = signing_owners
        await fund(w3, first.address)
        _, grant, _ = await registry_by_a_stranger(w3, chain_id)

        relay = await deploy_relay(w3, chain_id, first)
        assert "sender not an eoa" in await call_revert(w3, relay, grant, sender=first.address)
