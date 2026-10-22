"""Shared helpers: tempo-py signs the ``0x76`` tx, ``eth_contract`` builds calldata, ``AsyncWeb3`` transports."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Sequence

import pytest
from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo import Builder, Signer, add_fee_payer_signature, serialize, sign_transaction
from tempo.constants import (
    ALPHA_USD,
    BETA_USD,
    FEE_MANAGER_ADDRESS,
    PATH_USD,
    THETA_USD,
    TIP20_FACTORY_ADDRESS,
    TIP403_REGISTRY_ADDRESS,
)
from web3 import AsyncWeb3, Web3
from web3.middleware import ExtraDataToPOAMiddleware

from .abi import FEE, NONCE, TIP20, TIP20_FACTORY, TIP20_ROLES, TIP403
from .network import FAUCET_PRIVATE_KEY

# Every tempo tx the suite sends, at `--log-cli-level=INFO`: on a shared network the hash is
# what ties a failure to a block an explorer can show.
log = logging.getLogger(__name__)

# The four enshrined TIP-20 stablecoins, by symbol.
STABLECOINS = {"PATH_USD": PATH_USD, "ALPHA_USD": ALPHA_USD, "BETA_USD": BETA_USD, "THETA_USD": THETA_USD}

# ITIP403Registry.PolicyType. A whitelist authorizes only its members; a blacklist
# authorizes everyone except its members.
WHITELIST, BLACKLIST = 0, 1

ISSUER_ROLE = keccak(text="ISSUER_ROLE")  # TIP-20 mint role

MAX_UINT = 2**256 - 1  # unlimited ERC-20 approval
ZERO_ADDRESS = "0x" + "00" * 20
# TIP-1009 expiring nonces: the reserved nonce key (uint256 max). A tx on this key
# must carry nonce=0 and a valid_before within 30s; replay protection is hash-based.
EXPIRING_NONCE_KEY = 2**256 - 1
DEFAULT_GAS_LIMIT = 2_000_000
DEFAULT_MAX_PRIORITY_FEE_PER_GAS = 2_000_000_000
DEFAULT_MAX_FEE_PER_GAS = 100_000_000_000
# A tempo tx that writes new storage (DEX orders, token deploys) needs extra TIP-1060 state gas.
STATE_WRITE_GAS = 8_000_000
# What one account is given on a network funded by transfer: a few hundred writes' worth.
FUNDED_AMOUNT = 5_000_000
SET_CODE_GAS = 500_000  # a 7702 delegation, plus whatever the delegated code then does

# A timestamp no run reaches (year ~2096): a key that never expires, a fork that never activates.
# authorizeKey needs a real one, since 0 is ExpiryInPast.
FAR_FUTURE = 4_000_000_000


def key_restrictions(*, expiry=FAR_FUTURE, enforce_limits=False, limits=(), allow_any_calls=True, allowed_calls=()):
    """A KeyRestrictions ABI tuple: (expiry, enforceLimits, limits[], allowAnyCalls, allowedCalls[])."""
    return (expiry, enforce_limits, list(limits), allow_any_calls, list(allowed_calls))


# Minimal EVM fixtures: init code that deploys runtime returning 42, and the ERC-20 Transfer topic.
RETURN_42_INIT = "600a600c600039600a6000f3602a60005260206000f3"
RETURN_42_RUNTIME = bytes.fromhex("602a60005260206000f3")
TRANSFER_TOPIC = HexBytes("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")


def new_account():
    return Account.create()


def transfer_call(to: str, amount: int, token: str = PATH_USD) -> dict:
    """A TIP-20 ``transfer(to, amount)`` call for a tempo tx (``{to, data}``)."""
    return {"to": token, "data": ERC20.fns.transfer(to, amount).data}


def approve_call(spender: str, token: str = PATH_USD, amount: int = MAX_UINT) -> dict:
    """A TIP-20 ``approve(spender, amount)`` call for a tempo tx (``{to, data}``)."""
    return {"to": token, "data": ERC20.fns.approve(spender, amount).data}


async def suggested_max_fee(w3: AsyncWeb3, priority_fee: int = DEFAULT_MAX_PRIORITY_FEE_PER_GAS) -> int:
    """max_fee_per_gas comfortably above the current base fee (2x + priority)."""
    base_fee = (await w3.eth.get_block("latest")).get("baseFeePerGas") or 0
    return base_fee * 2 + priority_fee


def gas_cost_in_token(receipt) -> int:
    """Stablecoin fee for a tx: ``ceil(gasUsed * effectiveGasPrice / 1e12)`` (18-decimal gas, 6-decimal fee)."""
    wei = receipt["gasUsed"] * receipt["effectiveGasPrice"]
    return (wei + 10**12 - 1) // 10**12


def funder():
    """The account a network without a faucet is funded from: `$E2E_FUNDER_KEY`, else the first
    of `$MNEMONIC`. A devnet needs neither, since its faucet mints."""
    if key := os.environ.get("E2E_FUNDER_KEY"):
        return Account.from_key(key)
    if phrase := os.environ.get("MNEMONIC"):
        Account.enable_unaudited_hdwallet_features()
        return Account.from_mnemonic(phrase, account_path="m/44'/60'/0'/0/0")
    return None


async def fund(w3: AsyncWeb3, address: str, timeout: float = 60.0, amount: int = FUNDED_AMOUNT):
    """Fund ``address`` through the faucet's ``tempo_fundAddress``, or by transfer from [`funder`] on a
    network without one."""
    resp = await w3.provider.make_request("tempo_fundAddress", [AsyncWeb3.to_checksum_address(address)])
    if error := resp.get("error"):
        if error.get("code") != -32601:
            raise RuntimeError(f"tempo_fundAddress failed: {error}")
        account = funder()
        if account is None:
            raise RuntimeError(f"{w3.provider.endpoint_uri} has no faucet; set $MNEMONIC or $E2E_FUNDER_KEY")
        return await send_call(w3, await w3.eth.chain_id, account, PATH_USD, ERC20.fns.transfer(address, amount).data)
    result = resp.get("result")
    if isinstance(result, list):
        for tx_hash in result:
            await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    return result


async def funded(w3: AsyncWeb3):
    """A fresh account with a faucet balance. Called per subject, so a fixture will not do."""
    account = new_account()
    await fund(w3, account.address)
    return account


def build_tempo_tx(
    *,
    chain_id: int,
    calls: Sequence[dict],
    nonce: int = 0,
    nonce_key: int = 0,
    fee_token: str = PATH_USD,
    gas_limit: int = DEFAULT_GAS_LIMIT,
    max_fee_per_gas: int = DEFAULT_MAX_FEE_PER_GAS,
    max_priority_fee_per_gas: int = DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
    valid_before: int | None = None,
    valid_after: int | None = None,
    awaiting_fee_payer: bool = False,
):
    """An unsigned tempo (``0x76``) tx; each call is ``{to (None=create), value?, data?}``. With
    ``awaiting_fee_payer`` the sender's signature leaves out ``fee_token``, for a fee payer to sign."""
    builder = (
        Builder()
        .chain_id(chain_id)
        .gas_limit(gas_limit)
        .max_fee_per_gas(max_fee_per_gas)
        .max_priority_fee_per_gas(max_priority_fee_per_gas)
        .nonce(nonce)
        .nonce_key(nonce_key)
        .fee_token(fee_token)
    )
    if awaiting_fee_payer:
        builder.awaiting_fee_payer()
    if valid_before is not None:
        builder.valid_before(valid_before)
    if valid_after is not None:
        builder.valid_after(valid_after)
    for call in calls:
        builder.add_call(to=call.get("to") or "", value=call.get("value", 0), data=call.get("data", b""))
    return builder.build()


async def send_tempo_tx(w3: AsyncWeb3, tx, private_key: str, timeout: float = 60.0):
    """Sign, broadcast, and await the receipt for a tempo transaction."""
    return await send_signed(w3, sign_transaction(tx, Signer(private_key)), timeout=timeout)


async def send_signed(w3: AsyncWeb3, signed, timeout: float = 60.0):
    """Broadcast an already-signed tempo tx and await its receipt."""
    raw = serialize(signed)
    return await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw), timeout=timeout)


async def prepare_tx(
    w3: AsyncWeb3,
    chain_id: int,
    sender,
    calls: Sequence[dict],
    *,
    gas_limit: int = STATE_WRITE_GAS,
    fee_token: str = PATH_USD,
    awaiting_fee_payer: bool = False,
):
    """An unsigned tempo tx from ``sender`` over ``calls``, with nonce and fee filled, for a caller that
    signs or sends it some other way than ``send_calls``."""
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=await get_nonce(w3, sender.address),
        fee_token=fee_token,
        gas_limit=gas_limit,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=calls,
        awaiting_fee_payer=awaiting_fee_payer,
    )


async def sign_sponsored(w3: AsyncWeb3, chain_id: int, sender, payer, calls: Sequence[dict]):
    """A tx from ``sender`` over ``calls`` whose gas ``payer`` pays, signed by both."""
    tx = await prepare_tx(w3, chain_id, sender, calls, awaiting_fee_payer=True)
    return add_fee_payer_signature(sign_transaction(tx, Signer(sender.key.hex())), Signer(payer.key.hex()))


async def rpc_result(w3: AsyncWeb3, method: str, params: Sequence = ()):
    """``method``'s result; fails the test on a JSON-RPC error."""
    resp = await w3.provider.make_request(method, list(params))
    assert "error" not in resp, f"{method} failed: {resp['error']}"
    return resp["result"]


async def call_revert(w3: AsyncWeb3, to: str, data, *, sender: str | None = None) -> str:
    """``eth_call`` that must revert; the error message and data joined, so a test can match a precompile's
    error name or selector."""
    tx = {"to": to, "data": data if isinstance(data, str) else "0x" + bytes(data).hex()}
    if sender is not None:
        tx["from"] = sender
    resp = await w3.provider.make_request("eth_call", [tx, "latest"])
    err = resp.get("error")
    assert err is not None, f"expected revert, got result={resp.get('result')!r}"
    return f"{err.get('message', '')} {err.get('data', '') or ''}".strip()


async def send_calls(
    w3: AsyncWeb3,
    *,
    chain_id: int,
    private_key: str,
    calls: Sequence[dict],
    nonce: int | None = None,
    fee_token: str = PATH_USD,
    gas_limit: int = DEFAULT_GAS_LIMIT,
    max_priority_fee_per_gas: int = DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
    max_fee_per_gas: int | None = None,
):
    """Build, sign and send a tempo tx over ``calls``, filling the nonce and a fee above the base fee."""
    sender = Account.from_key(private_key).address
    if nonce is None:
        nonce = await get_nonce(w3, sender)
    if max_fee_per_gas is None:
        max_fee_per_gas = await suggested_max_fee(w3, max_priority_fee_per_gas)
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=nonce,
        fee_token=fee_token,
        gas_limit=gas_limit,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        max_fee_per_gas=max_fee_per_gas,
        calls=calls,
    )
    receipt = await send_tempo_tx(w3, tx, private_key)
    log.info(
        "tx %s block %s status %s gas %s from %s",
        receipt["transactionHash"].hex(),
        receipt["blockNumber"],
        receipt["status"],
        receipt["gasUsed"],
        sender,
    )
    return receipt


async def send_call(
    w3: AsyncWeb3,
    chain_id: int,
    signer,
    to: str,
    data,
    *,
    gas_limit: int = STATE_WRITE_GAS,
    fee_token: str = PATH_USD,
):
    """Send a single-call tempo tx from ``signer`` (a local account), asserting success."""
    receipt = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=signer.key.hex(),
        gas_limit=gas_limit,
        fee_token=fee_token,
        calls=[{"to": to, "data": data}],
    )
    assert receipt["status"] == 1
    return receipt


async def send_type_2(w3: AsyncWeb3, account, to: str, data, *, gas: int | None = None, value: int = 0):
    """A plain EIP-1559 tx from ``account``, asserting success. It names no fee token, so the payer's own
    setting decides; gas defaults to the estimate, since TIP-1016 charges per new slot."""
    if gas is None:
        gas = await w3.eth.estimate_gas({"to": to, "from": account.address, "data": data, "value": value})
    tx = {
        "to": to,
        "data": data,
        "value": value,
        "nonce": await w3.eth.get_transaction_count(account.address),
        "chainId": await w3.eth.chain_id,
        "gas": gas,
        "maxFeePerGas": await suggested_max_fee(w3),
        "maxPriorityFeePerGas": DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
        "type": 2,
    }
    raw = Account.sign_transaction(tx, account.key).raw_transaction
    receipt = await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw))
    assert receipt["status"] == 1, receipt
    return receipt


async def latest_timestamp(w3: AsyncWeb3) -> int:
    return (await w3.eth.get_block("latest"))["timestamp"]


async def active_forks(w3: AsyncWeb3) -> set[str]:
    """The hardforks the node reports as active, named as ``tempo_forkSchedule`` names them,
    so a test can state what each fork brings instead of pinning one binary."""
    schedule = (await rpc_result(w3, "tempo_forkSchedule"))["schedule"]
    return {fork["name"].upper() for fork in schedule if fork["active"]}


async def wait_for_block(w3: AsyncWeb3, number: int, *, timeout: float = 300.0, poll: float = 0.5) -> int:
    deadline = asyncio.get_running_loop().time() + timeout
    while (height := await w3.eth.block_number) < number:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"chain stalled at block {height} after {timeout}s, waiting for {number}")
        await asyncio.sleep(poll)
    return height


def poll_height(rpc_url: str) -> int:
    try:
        return Web3(Web3.HTTPProvider(rpc_url)).eth.block_number
    except Exception:
        return -1


def wait_height(rpc_url: str, target: int, timeout: float = 90.0) -> int:
    deadline = time.time() + timeout
    while poll_height(rpc_url) < target and time.time() < deadline:
        time.sleep(1.0)
    return poll_height(rpc_url)


def token_from_receipt(receipt, factory: str = TIP20_FACTORY_ADDRESS) -> str:
    """The new token address from the factory's TokenCreated event (indexed topic 1)."""
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == factory.lower())
    return AsyncWeb3.to_checksum_address(HexBytes(log["topics"][1])[-20:])


async def create_token(
    w3: AsyncWeb3,
    *,
    chain_id: int,
    admin,
    quote: str = PATH_USD,
    name: str = "TUSD",
    mint=None,
    salt: bytes = bytes(32),
):
    """A TIP-20 from the factory, optionally minting ``(holder, amount)``; its address derives from ``admin``
    and ``salt``."""
    created = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {
                "to": TIP20_FACTORY_ADDRESS,
                "data": TIP20_FACTORY.fns.createToken(name, name, "USD", quote, admin.address, salt).data,
            }
        ],
    )
    assert created["status"] == 1
    token = token_from_receipt(created)
    if mint is not None:
        holder, amount = mint
        minted = await send_calls(
            w3,
            chain_id=chain_id,
            private_key=admin.key.hex(),
            gas_limit=STATE_WRITE_GAS,
            calls=[
                {"to": token, "data": TIP20_ROLES.fns.grantRole(ISSUER_ROLE, admin.address).data},
                {"to": token, "data": ERC20.fns.mint(holder, amount).data},
            ],
        )
        assert minted["status"] == 1
    return token


async def blacklist_token(w3: AsyncWeb3, *, chain_id: int, admin, token: str, blocked: str) -> int:
    """Bind ``token``'s transfer policy to a fresh BLACKLIST policy that rejects ``blocked``."""
    policy_id = await TIP403.fns.policyIdCounter().call(w3)
    await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": TIP403_REGISTRY_ADDRESS, "data": TIP403.fns.createPolicy(admin.address, BLACKLIST).data},
            {
                "to": TIP403_REGISTRY_ADDRESS,
                "data": TIP403.fns.modifyPolicyBlacklist(policy_id, blocked, True).data,
            },
            {"to": token, "data": TIP20.fns.changeTransferPolicyId(policy_id).data},
        ],
    )
    assert await TIP20.fns.transferPolicyId().call(w3, to=token) == policy_id
    assert not await TIP403.fns.isAuthorized(policy_id, blocked).call(w3)
    return policy_id


async def fund_token(
    w3: AsyncWeb3, *, chain_id: int, to: str, token: str, amount: int, funder_pk: str = FAUCET_PRIVATE_KEY
):
    """Transfer ``token`` from the prefunded genesis account, to test paying gas in a non-PATH_USD token."""
    return await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funder_pk,
        fee_token=token,
        calls=[{"to": token, "data": ERC20.fns.transfer(to, amount).data}],
    )


async def seed_fee_pool(
    w3: AsyncWeb3,
    *,
    chain_id: int,
    user_token: str,
    validator_token: str = PATH_USD,
    amount: int = 50_000_000_000,
    funder_pk: str = FAUCET_PRIVATE_KEY,
):
    """Mint a FeeAMM pool so gas can be paid in ``user_token``; the dev genesis seeds only the ALPHA_USD pool."""
    funder = Account.from_key(funder_pk).address
    return await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funder_pk,
        fee_token=validator_token,
        gas_limit=STATE_WRITE_GAS,
        calls=[
            approve_call(FEE_MANAGER_ADDRESS, validator_token, amount * 4),
            {"to": FEE_MANAGER_ADDRESS, "data": FEE.fns.mint(user_token, validator_token, amount, funder).data},
        ],
    )


async def send_set_code_tx(
    w3: AsyncWeb3,
    *,
    chain_id: int,
    sponsor,
    authority,
    delegate: str,
    auth_nonce: int,
    to: str,
    data=b"",
    gas: int = SET_CODE_GAS,
):
    """A type-0x04 tx from ``sponsor`` delegating ``authority``, which signs the authorization, to ``delegate``."""
    signed_auth = Account.sign_authorization(
        {"chainId": chain_id, "address": AsyncWeb3.to_checksum_address(delegate), "nonce": auth_nonce}, authority.key
    )
    tx = {
        "to": AsyncWeb3.to_checksum_address(to),
        "value": 0,
        "data": data,
        "nonce": await w3.eth.get_transaction_count(sponsor.address),
        "chainId": chain_id,
        "gas": gas,
        "maxFeePerGas": await suggested_max_fee(w3),
        "maxPriorityFeePerGas": DEFAULT_MAX_PRIORITY_FEE_PER_GAS,
        "authorizationList": [signed_auth],
    }
    signed = Account.sign_transaction(tx, sponsor.key)
    return await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(signed.raw_transaction))


async def deploy_contract(w3: AsyncWeb3, *, chain_id: int, private_key: str, bytecode, nonce: int | None = None):
    """Deploy ``bytecode`` via a tempo create tx; return ``(receipt, address)``."""
    if isinstance(bytecode, str):
        bytecode = bytes.fromhex(bytecode[2:] if bytecode.startswith("0x") else bytecode)
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=private_key, calls=[{"to": None, "data": bytecode}], nonce=nonce
    )
    return receipt, receipt.get("contractAddress")


async def get_nonce(w3: AsyncWeb3, address: str, nonce_key: int = 0) -> int:
    """Current nonce for ``address``: the account nonce for key 0, else the Nonce precompile."""
    if nonce_key == 0:
        return await w3.eth.get_transaction_count(AsyncWeb3.to_checksum_address(address))
    return await NONCE.fns.getNonce(address, nonce_key).call(w3)


# ── Devnet clusters and RPC transports ─────────────────────────────────────


@asynccontextmanager
async def rpc(node):
    """An RPC client for ``node`` or its URL, disconnected on the way out."""
    client = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(getattr(node, "rpc_url", node)))
    # A consensus node's headers carry DKG payloads in extraData, past web3.py's 32-byte cap.
    client.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    try:
        yield client
    finally:
        await client.provider.disconnect()


@asynccontextmanager
async def rpc_all(nodes):
    """A client per node, all disconnected on the way out."""
    async with AsyncExitStack() as stack:
        yield [await stack.enter_async_context(rpc(node)) for node in nodes]


def cluster_fixture(name: str, validators: int, *, env: dict[str, str] | None = None, warmup: float = 0.0):
    """A module-scoped fixture yielding a started ``validators``-node devnet, ``env`` set for its nodes and
    ``warmup`` seconds to settle; skips on backends without ``make_cluster``."""

    @pytest.fixture(scope="module")
    def _cluster(request, driver, tmp_path_factory):
        if not hasattr(driver, "make_cluster"):
            pytest.skip(f"backend {driver.name!r} has no make_cluster()")
        base = tmp_path_factory.mktemp(name)
        nodes = driver.make_cluster(base, validators)
        previous = {key: os.environ.get(key) for key in env or {}}
        os.environ.update(env or {})
        try:
            for node in nodes:
                node.start()
            for node in nodes:
                node.wait_for_rpc(timeout=90.0, want_block=1)
            time.sleep(warmup)
            yield nodes
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for node in nodes:
                node.stop()
            if request.config.getoption("--clean-data"):
                shutil.rmtree(base, ignore_errors=True)

    return _cluster
