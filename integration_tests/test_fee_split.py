"""Genesis names each validator's fee recipient: an address CreateX reserves, empty until the
nvnmchain-contracts ``FeeSplitter`` is deployed there and divides every fee of that validator's blocks since
block one."""

import pytest
from eth_abi import encode
from eth_contract import Contract
from eth_contract.erc20 import ERC20
from eth_utils import keccak, to_checksum_address
from tempo.constants import PATH_USD

from .abi import FEE
from .anchoring import LAYOUT
from .conftest import supervisord_net
from .utils import call_revert, emitted, mnemonic_account, rpc, send_call, wait_for_block

pytestmark = [pytest.mark.tempo, pytest.mark.consensus]

CHAIN_ID = 1337  # the localnet's: the addresses are bound to it before the chain exists
CREATEX = to_checksum_address("0xba5Ed099633D3B313e4D5F7bdc1305d3c28ba5Ed")
DEPLOYER, OWNER, PAYER = (mnemonic_account(i) for i in (10, 11, 12))
DEVSHARE, BUYBACK = to_checksum_address(f"0x{0xD0:040x}"), to_checksum_address(f"0x{0xB0:040x}")
OPERATORS = [to_checksum_address(f"0x{0x101 + i:040x}") for i in range(2)]  # one per validator
_PROXY_HASH = bytes.fromhex("21c35dbe1b344a2488cf3321d6ce542f8e9f305544ff09e4993a62319a497c1f")


def _reserved(i: int) -> tuple[bytes, str]:
    """DEPLOYER's salt for validator ``i``, bound to this chain (its address, flag 0x01, 11 free bytes),
    and the address CreateX deploys it to: the proxy's first CREATE."""
    salt = bytes.fromhex(DEPLOYER.address[2:]) + b"\x01" + (i + 1).to_bytes(11, "big")
    guarded = keccak(encode(["address", "uint256", "bytes32"], [DEPLOYER.address, CHAIN_ID, salt]))
    proxy = keccak(b"\xff" + bytes.fromhex(CREATEX[2:]) + guarded + _PROXY_HASH)[12:]
    return salt, to_checksum_address(keccak(b"\xd6\x94" + proxy + b"\x01")[12:])


SALTS, SPLITTERS = zip(*map(_reserved, range(len(OPERATORS))))

CREATEX_ABI = Contract.from_abi(["function deployCreate3(bytes32 salt, bytes initCode) payable"], to=CREATEX)
SPLITTER_ABI = [
    "function split()",
    "function commence()",
    "function claim()",
    "function held() view returns (uint256)",
    "event Split(uint256 devshare, uint256 buyback, uint256 validator)",
]
INIT_CODE = bytes.fromhex((LAYOUT / "fee-splitter.init.bin").read_text().strip().removeprefix("0x"))
NOT_COMMENCED = "0x" + keccak(text="NotCommenced()")[:4].hex()


@pytest.fixture(scope="module")
def net(request, tmp_path_factory):
    yield from supervisord_net(
        request,
        tmp_path_factory.mktemp("fee-split"),
        len(OPERATORS),
        patch_genesis_flags=["--fee-recipients", ",".join(SPLITTERS)],
    )


async def test_each_validator_s_fees_accrue_to_its_own_splitter(net):
    async with rpc(net.node_rpc_url("node0")) as w3, rpc(net.node_rpc_url("node1")) as w3_1:

        async def balance(address):
            return await ERC20.fns.balanceOf(address).call(w3, to=PATH_USD)

        async def ledger(splitter):
            return await FEE.fns.collectedFees(splitter, PATH_USD).call(w3)

        assert await w3.eth.chain_id == CHAIN_ID
        # Every block pays its proposer's splitter. Sending through both nodes in turn gives each a
        # transaction of its own to propose, whether or not their pools gossip; each waits to hold the
        # last one's block, or it would sign the same nonce again.
        paid = {"blockNumber": 0}
        for i in range(30):
            node = (w3, w3_1)[i % 2]
            await wait_for_block(node, paid["blockNumber"], timeout=30, poll=0.1)
            paid = await send_call(node, CHAIN_ID, PAYER, PATH_USD, ERC20.fns.transfer(OWNER.address, 1).data)
            if all([await ledger(s) for s in SPLITTERS]):
                break
        else:
            pytest.fail("one validator proposed none of 30 transactions")
        miners = {(await w3.eth.get_block(n))["miner"] for n in range(1, paid["blockNumber"] + 1)}
        assert miners == set(SPLITTERS)
        # Anyone may pay a ledger out before its splitter exists: it lands at the empty address.
        await send_call(w3, CHAIN_ID, PAYER, FEE.fns.distributeFees(SPLITTERS[0], PATH_USD))
        assert await balance(SPLITTERS[0]) > 0

        splitters = []
        for salt, splitter, operator in zip(SALTS, SPLITTERS, OPERATORS):
            args = encode(
                ["address", "address", "address", "uint256", "uint256", "address"],
                [OWNER.address, DEVSHARE, BUYBACK, 2_500, 2_500, operator],
            )
            deploy = CREATEX_ABI.fns.deployCreate3(salt, INIT_CODE + args)
            # From anyone else CreateX takes the salt as a random one, so the code lands elsewhere.
            await send_call(w3, CHAIN_ID, PAYER, deploy)
            assert await w3.eth.get_code(splitter) == b""
            await send_call(w3, CHAIN_ID, DEPLOYER, deploy)
            assert await w3.eth.get_code(splitter) != b""
            splitters.append(Contract.from_abi(SPLITTER_ABI, to=splitter))

        # Each splitter divides only its own validator's fees: 25% each to devshare and buybacks, the
        # rest held for the validator.
        for splitter in splitters:
            fresh = await balance(splitter.tx["to"]) + await ledger(splitter.tx["to"])
            dev_before, buy_before = await balance(DEVSHARE), await balance(BUYBACK)
            [(dev, buy, kept)] = emitted(await send_call(w3, CHAIN_ID, PAYER, splitter.fns.split()), splitter, "Split")
            assert dev == buy == fresh * 2_500 // 10_000
            assert kept == fresh - dev - buy == await splitter.fns.held().call(w3)
            assert (await balance(DEVSHARE) - dev_before, await balance(BUYBACK) - buy_before) == (dev, buy)

        first = splitters[0]
        held = await first.fns.held().call(w3)
        assert NOT_COMMENCED in await call_revert(w3, first.fns.claim(), sender=PAYER.address)
        await send_call(w3, CHAIN_ID, OWNER, first.fns.commence())
        # A claim splits first: here whatever its validator charged since.
        [(_, _, more)] = emitted(await send_call(w3, CHAIN_ID, PAYER, first.fns.claim()), first, "Split")
        assert await balance(OPERATORS[0]) == held + more
        assert await first.fns.held().call(w3) == 0
