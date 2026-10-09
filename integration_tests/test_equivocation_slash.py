"""Evidence of a double sign costs a bond end to end: the node's registry reads the evidence, the
staking contract slashes on its answer, and the bridge seizes as much on Ethereum.

Runs on its own dev node, whose genesis makes votes attributable: that is what turns the
registry's evidence check on. No validator signs twice by itself, so the two votes are signed by
hand with a key the registry holds for the bonded address."""

import json

import pytest
from Crypto.PublicKey import ECC
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from tempo.constants import VALIDATOR_CONFIG_V2_ADDRESS

from .abi import EQUIVOCATION, NVNM_LOCKBOX, STAKING
from .anvil import MILLION
from .bridge import DEPLOYER, SEIZE_SINK, lock
from .evidence import double_notarize
from .network import FAUCET_PRIVATE_KEY, dev_node, free_port
from .staking import bytecode, create3
from .test_bridge import account, bridge, candidates  # noqa: F401  (bridge is the fixture)
from .test_validator_config import join
from .utils import call_revert, cs, until

pytestmark = pytest.mark.requires("tempo-native")

# A tenth of the bond, for evidence however old: the dev chain's epochs are the genesis' to size.
BPS, EPOCHS = 1_000, 2**32


@pytest.fixture(scope="module")
def tempo(tmp_path_factory):
    """In place of the session's dev node: one whose votes are attributable from genesis."""
    node = dev_node(tmp_path_factory.mktemp("equivocation"), log_name="equivocation.log")
    genesis = json.loads(node.genesis.read_text())
    genesis["config"]["attributableVotesTime"] = 0
    node.genesis.write_text(json.dumps(genesis))
    try:
        node.start().wait_for_rpc()
        yield node
    finally:
        node.stop()


async def test_evidence_slashes_a_bond_and_seizes_it_on_ethereum(bridge, tempo):  # noqa: F811
    b, staking, w3, eth = bridge, bridge.staking, bridge.l1.w3, bridge.eth.w3
    chain_id, bond = await w3.eth.chain_id, b.units(MILLION)
    await staking.setup_election(DEPLOYER, [], seats=1, unbonding=2)  # slashing opens with it
    await staking.send(DEPLOYER, STAKING.fns.setCandidacyBond(bond))
    v = await account(b)
    await lock(b, v.key.hex(), MILLION)
    await until("the bond to stand its validator", lambda: candidates(b), want=[v.address])

    # The staking that stands was deployed before evidence could slash: its owner upgrades it.
    implementation = await create3(w3, chain_id, DEPLOYER, bytecode("staking_implementation"))
    await staking.send(DEPLOYER, STAKING.fns.upgradeToAndCall(implementation, b""))
    assert await staking.call(STAKING.fns.bondOf(v.address)) == bond, "the bond outlives the upgrade"
    await staking.send(DEPLOYER, STAKING.fns.setEquivocation(BPS, EPOCHS))

    # The registry holds a consensus key for the bonded address, and that key signs twice.
    owner, key = Account.from_key(FAUCET_PRIVATE_KEY), ECC.generate(curve="ed25519")
    await join(w3, chain_id, owner, v.address, f"127.0.0.1:{free_port()}", key=key)
    epoch = await w3.eth.block_number // json.loads(tempo.genesis.read_text())["config"].get("epochLength", 1)
    genesis = bytes((await w3.eth.get_block(0))["hash"])
    evidence = double_notarize(key, chain_id, genesis, epoch)
    named, *_ = await EQUIVOCATION.fns.equivocator(evidence).call(w3, to=VALIDATOR_CONFIG_V2_ADDRESS)
    assert cs(named) == v.address

    # Anyone brings it, and pays the bridge for the seizure.
    reporter = await account(b)
    eth_bond, sunk = NVNM_LOCKBOX.fns.bondOf(v.address), ERC20.fns.balanceOf(SEIZE_SINK)
    before = await sunk.call(eth, to=b.nvnm)
    await staking.send(reporter, STAKING.fns.slashEquivocation(evidence))
    assert await staking.call(STAKING.fns.bondOf(v.address)) == bond - bond * BPS // 10_000
    await until("the seizure on Ethereum", lambda: eth_bond.call(eth, to=b.lockbox), want=MILLION * 9 // 10)
    assert await sunk.call(eth, to=b.nvnm) - before == MILLION // 10

    # The round is paid for: the same evidence slashes nothing more.
    again = STAKING.fns.slashEquivocation(evidence).data
    reason = await call_revert(w3, staking.address, again, sender=reporter.address)
    assert "AlreadySlashed" in reason or "0x" + keccak(text="AlreadySlashed()")[:4].hex() in reason
