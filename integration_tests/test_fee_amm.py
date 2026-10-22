"""Fee AMM: paying gas in a non-validator stablecoin swaps through the FeeManager pool."""

import asyncio

import pytest
from eth_abi import encode
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo import Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, FEE_MANAGER_ADDRESS, PATH_USD, THETA_USD
from web3 import Web3
from web3.exceptions import Web3RPCError

from .abi import FEE, TIP20, TIP20_ROLES
from .conftest import supervisord_net
from .network import FAUCET_PRIVATE_KEY, dev_node
from .utils import (
    create_token,
    fund_token,
    gas_cost_in_token,
    new_account,
    prepare_tx,
    rpc,
    seed_fee_pool,
    send_call,
    send_calls,
    send_type_2,
    transfer_call,
)

pytestmark = pytest.mark.tempo


async def test_mint_seeds_pool_for_ungenesised_token(w3, chain_id):
    """A stablecoin the genesis didn't seed (THETA) becomes gas-payable after FeeManager.mint."""
    await seed_fee_pool(w3, chain_id=chain_id, user_token=THETA_USD)
    _, reserve_validator = await FEE.fns.getPool(THETA_USD, PATH_USD).call(w3)
    assert reserve_validator > 0  # validator (PATH) side now funded, enabling THETA->PATH swaps


async def test_pool_id_is_deterministic(w3):
    pool_id = await FEE.fns.getPoolId(ALPHA_USD, PATH_USD).call(w3)
    assert pool_id == keccak(encode(["address", "address"], [ALPHA_USD, PATH_USD]))


async def test_pool_has_reserves(w3):
    _, reserve_validator = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3)
    assert reserve_validator > 0  # PATH side is seeded in genesis


async def test_set_user_token(w3, chain_id, funded_account):
    await send_call(w3, chain_id, funded_account, FEE_MANAGER_ADDRESS, FEE.fns.setUserToken(ALPHA_USD).data)
    stored = await FEE.fns.userTokens(funded_account.address).call(w3)
    assert HexBytes(stored) == HexBytes(ALPHA_USD)


async def test_fee_in_non_validator_token_moves_pool(w3, chain_id):
    """Gas paid in ALPHA (the validator wants PATH) is swapped via the AMM, shifting reserves."""
    before = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3)
    user = new_account()
    await fund_token(w3, chain_id=chain_id, to=user.address, token=ALPHA_USD, amount=50_000_000_000)

    calls = [transfer_call(new_account().address, 1, ALPHA_USD)]
    receipt = await send_calls(w3, chain_id=chain_id, private_key=user.key.hex(), fee_token=ALPHA_USD, calls=calls)
    assert receipt["status"] == 1

    after = await FEE.fns.getPool(ALPHA_USD, PATH_USD).call(w3)
    assert after[0] > before[0]  # ALPHA (user token) reserve grew
    assert after[1] < before[1]  # PATH (validator token) reserve shrank


async def test_creator_seeds_its_own_pool_and_pays_gas_in_it(w3, chain_id, funded_account):
    """Self-service e2e: one ordinary account creates a TIP-20, seeds its fee pool, then pays gas in it."""
    creator, minted = funded_account, 10_000_000
    token = await create_token(w3, chain_id=chain_id, admin=creator, mint=(creator.address, minted))
    await seed_fee_pool(w3, chain_id=chain_id, user_token=token, funder_pk=creator.key.hex())

    pool_id = await FEE.fns.getPoolId(token, PATH_USD).call(w3)
    assert await FEE.fns.liquidityBalances(pool_id, creator.address).call(w3) > 0
    # mint() only pulls validator side, so seeding cost the creator none of its own token
    assert await ERC20.fns.balanceOf(creator.address).call(w3, to=token) == minted

    before = await FEE.fns.getPool(token, PATH_USD).call(w3)
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=creator.key.hex(),
        fee_token=token,
        calls=[transfer_call(new_account().address, 1000, token)],
    )
    assert receipt["status"] == 1
    after = await FEE.fns.getPool(token, PATH_USD).call(w3)

    fee = gas_cost_in_token(receipt)
    assert await ERC20.fns.balanceOf(creator.address).call(w3, to=token) == minted - 1000 - fee
    assert after[0] - before[0] == fee  # fee landed in the pool the creator seeded
    assert after[1] < before[1]  # and PATH left it for validator


async def test_insufficient_liquidity_names_the_fee_token(w3, chain_id, funded_account):
    """Paying gas in a no-pool token is rejected with an error that names the fee token."""
    payer = new_account()
    # A fresh USD TIP-20 with no fee pool, payer holds enough that the swap, not the balance, is what fails.
    token = await create_token(w3, chain_id=chain_id, admin=funded_account, mint=(payer.address, 10_000_000_000))

    tx = await prepare_tx(w3, chain_id, payer, [transfer_call(new_account().address, 1, token)], fee_token=token)
    raw = serialize(sign_transaction(tx, Signer(payer.key.hex())))
    resp = await w3.provider.make_request("eth_sendRawTransaction", [raw])

    msg = (resp.get("error") or {}).get("message", "")
    assert "insufficient liquidity in FeeAMM pool to swap fee tokens" in msg, resp
    assert "(required:" in msg, resp  # required amount
    assert token.lower() in msg.lower(), msg  # identifies the offending fee token


@pytest.mark.slow
class TestDeploymentGasToken:
    """Fees on xtask's temporary gas token, before any stablecoin is bridged. On a dev node every
    block's fee recipient is address zero, which has no key, so only the payer's side is tested."""

    ISSUER = Account.from_key(FAUCET_PRIVATE_KEY)
    PAYER = Account.from_key("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")  # a genesis account

    @pytest.fixture
    async def gas_token_w3(self, tmp_path):
        node = dev_node(tmp_path, log_name="deployment-gas-token.log", gas_token_admin=self.ISSUER.address)
        try:
            node.start().wait_for_rpc()
            async with rpc(node) as w3:
                yield w3
        finally:
            node.stop()

    async def test_a_payer_leaves_it_only_through_a_pool(self, gas_token_w3):
        w3 = gas_token_w3
        cs, payer = Web3.to_checksum_address, self.PAYER
        token = cs(await FEE.fns.userTokens(payer.address).call(w3))
        recipient = (await w3.eth.get_block("latest"))["miner"]
        assert cs(await FEE.fns.validatorTokens(recipient).call(w3)) == token

        # Gas comes out of the temporary token; nUSD moves only by the transfer.
        balance = ERC20.fns.balanceOf(payer.address)
        gas, usd = await balance.call(w3, to=token), await balance.call(w3, to=PATH_USD)
        await send_type_2(w3, payer, PATH_USD, ERC20.fns.transfer(new_account().address, 1).data)
        assert await balance.call(w3, to=token) < gas
        assert await balance.call(w3, to=PATH_USD) == usd - 1

        # Paying in nUSD needs a pool into the token the recipient takes; the issuer seeds one.
        switch = FEE.fns.setUserToken(PATH_USD).data
        with pytest.raises(Web3RPCError, match="insufficient liquidity"):
            await send_type_2(w3, payer, FEE_MANAGER_ADDRESS, switch)
        liquidity = 10**10
        await send_type_2(w3, self.ISSUER, token, ERC20.fns.approve(FEE_MANAGER_ADDRESS, liquidity).data)
        await send_type_2(
            w3, self.ISSUER, FEE_MANAGER_ADDRESS, FEE.fns.mint(PATH_USD, token, liquidity, self.ISSUER.address).data
        )
        await send_type_2(w3, payer, FEE_MANAGER_ADDRESS, switch)

        pool = FEE.fns.getPool(PATH_USD, token)
        gas, reserve = await balance.call(w3, to=token), (await pool.call(w3))[0]
        await send_type_2(w3, payer, PATH_USD, ERC20.fns.transfer(new_account().address, 1).data)
        assert await balance.call(w3, to=token) == gas, "no temporary token spent"
        assert (await pool.call(w3))[0] > reserve, "the nUSD fee went through the pool"


def _dev_account(index: int):
    """An account of the dev mnemonic, which funds the localnet's genesis and names its validators."""
    Account.enable_unaudited_hdwallet_features()
    return Account.from_mnemonic(
        "test test test test test test test test test test test junk", account_path=f"m/44'/60'/0'/0/{index}"
    )


@pytest.mark.consensus
@pytest.mark.slow
class TestDeploymentGasTokenOnValidators:
    """The move off xtask's temporary gas token once nUSD is bridged, with each block's fee going to
    its proposer. Genesis puts the accounts and validators on the token; anyone else is on nUSD."""

    VALIDATORS = 4  # the localnet names the dev mnemonic's accounts 1..4

    @pytest.fixture(scope="class")
    def gas_token_net(self, request, tmp_path_factory):
        flags = ["--deployment-gas-token", "--deployment-gas-token-admin", _dev_account(0).address]
        base = tmp_path_factory.mktemp("gas-token-net")
        yield from supervisord_net(request, base, self.VALIDATORS, patch_genesis_flags=flags)

    async def test_the_switch_to_nusd_needs_no_fork(self, gas_token_net):
        async with rpc(gas_token_net.node_rpc_url("node0")) as w3:
            cs = Web3.to_checksum_address
            chain_id = await w3.eth.chain_id
            admin, payer = _dev_account(0), _dev_account(10)
            validators = [_dev_account(i) for i in range(1, self.VALIDATORS + 1)]
            transfer = ERC20.fns.transfer(new_account().address, 1).data

            token = cs(await FEE.fns.userTokens(payer.address).call(w3))
            for validator in validators:
                assert cs(await FEE.fns.validatorTokens(validator.address).call(w3)) == token
            newcomer = new_account()
            await send_type_2(w3, payer, PATH_USD, ERC20.fns.transfer(newcomer.address, 10**9).data)
            with pytest.raises(Web3RPCError, match="insufficient liquidity"):
                await send_type_2(w3, newcomer, PATH_USD, transfer)

            # 1. The admin seeds the nUSD -> token pool with the token alone.
            liquidity = 10**10
            await send_type_2(w3, admin, token, ERC20.fns.approve(FEE_MANAGER_ADDRESS, liquidity).data)
            await send_type_2(
                w3, admin, FEE_MANAGER_ADDRESS, FEE.fns.mint(PATH_USD, token, liquidity, admin.address).data
            )
            await send_type_2(w3, newcomer, PATH_USD, transfer)

            # 2. Every account on the token moves to nUSD, validators and admin included.
            switch = FEE.fns.setUserToken(PATH_USD).data
            for account in (admin, payer, *validators):
                await send_type_2(w3, account, FEE_MANAGER_ADDRESS, switch)

            # 3. Each validator takes nUSD at once; the call reverts in a block its sender proposes, so those resend.
            calls = [{"to": FEE_MANAGER_ADDRESS, "data": FEE.fns.setValidatorToken(PATH_USD).data}]
            pending = validators
            for _ in range(5):
                receipts = await asyncio.gather(
                    *(send_calls(w3, chain_id=chain_id, private_key=v.key.hex(), calls=calls) for v in pending)
                )
                reverted = [(v, r) for v, r in zip(pending, receipts, strict=True) if r["status"] == 0]
                for validator, receipt in reverted:
                    miner = (await w3.eth.get_block(receipt["blockNumber"]))["miner"]
                    assert miner == validator.address, f"{validator.address} reverted in a block it did not propose"
                pending = [v for v, _ in reverted]
                if not pending:
                    break
            assert not pending, f"still proposing their own blocks after 5 rounds: {[v.address for v in pending]}"
            for validator in validators:
                assert cs(await FEE.fns.validatorTokens(validator.address).call(w3)) == PATH_USD

            pool = FEE.fns.getPool(PATH_USD, token)
            reserves = await pool.call(w3)
            assert reserves[0] > 0, "nUSD fees collected while the set was split"
            await send_type_2(w3, payer, PATH_USD, transfer)
            assert await pool.call(w3) == reserves, "no fee goes through the pool"

            # 4. The admin takes the collected nUSD out, then pauses the token.
            pool_id = await FEE.fns.getPoolId(PATH_USD, token).call(w3)
            shares = await FEE.fns.liquidityBalances(pool_id, admin.address).call(w3)
            await send_type_2(w3, admin, FEE_MANAGER_ADDRESS, FEE.fns.burn(PATH_USD, token, shares, admin.address).data)
            assert (await pool.call(w3))[0] < reserves[0]
            pause_role = await TIP20.fns.PAUSE_ROLE().call(w3, to=token)
            await send_type_2(w3, admin, token, TIP20_ROLES.fns.grantRole(pause_role, admin.address).data)
            await send_type_2(w3, admin, token, TIP20.fns.pause().data)
            assert await TIP20.fns.paused().call(w3, to=token)
