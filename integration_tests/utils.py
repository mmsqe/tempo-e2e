"""Shared helpers: tempo-py signs the ``0x76`` tx, ``eth_contract`` builds calldata, ``AsyncWeb3`` transports."""

from __future__ import annotations

from typing import Sequence

from eth_account import Account
from eth_contract.erc20 import ERC20
from eth_utils import keccak
from hexbytes import HexBytes
from tempo import Builder, Signer, serialize, sign_transaction
from tempo.constants import (
    ALPHA_USD,
    BETA_USD,
    FEE_MANAGER_ADDRESS,
    NONCE_ADDRESS,
    PATH_USD,
    THETA_USD,
    TIP20_FACTORY_ADDRESS,
)
from web3 import AsyncWeb3

from .abi import FEE, NONCE, TIP20_FACTORY, TIP20_ROLES
from .network import FAUCET_PRIVATE_KEY

# The four enshrined TIP-20 stablecoins, by symbol.
STABLECOINS = {"PATH_USD": PATH_USD, "ALPHA_USD": ALPHA_USD, "BETA_USD": BETA_USD, "THETA_USD": THETA_USD}

ISSUER_ROLE = keccak(text="ISSUER_ROLE")  # TIP-20 mint role

MAX_UINT = 2**256 - 1  # unlimited ERC-20 approval
DEFAULT_GAS_LIMIT = 2_000_000
DEFAULT_MAX_PRIORITY_FEE_PER_GAS = 2_000_000_000
DEFAULT_MAX_FEE_PER_GAS = 100_000_000_000
# A tempo tx that writes new storage (DEX orders, token deploys) needs extra TIP-1060 state gas.
STATE_WRITE_GAS = 8_000_000

# Default KeyRestrictions expiry (year ~2096): the on-chain authorizeKey path needs a real
# timestamp (0 is ExpiryInPast), unlike the inline sign path's never-expire sentinel.
NEVER_EXPIRES = 4_000_000_000


def key_restrictions(*, expiry=NEVER_EXPIRES, enforce_limits=False, limits=(), allow_any_calls=True, allowed_calls=()):
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


async def suggested_max_fee(w3: AsyncWeb3, priority_fee: int = DEFAULT_MAX_PRIORITY_FEE_PER_GAS) -> int:
    """max_fee_per_gas comfortably above the current base fee (2x + priority)."""
    base_fee = (await w3.eth.get_block("latest")).get("baseFeePerGas") or 0
    return base_fee * 2 + priority_fee


def gas_cost_in_token(receipt) -> int:
    """Stablecoin fee for a tx: ``ceil(gasUsed * effectiveGasPrice / 1e12)`` (18-decimal gas, 6-decimal fee)."""
    wei = receipt["gasUsed"] * receipt["effectiveGasPrice"]
    return (wei + 10**12 - 1) // 10**12


async def fund(w3: AsyncWeb3, address: str, timeout: float = 60.0):
    """Fund ``address`` with the faucet TIP-20 via ``tempo_fundAddress``; await any returned txs."""
    resp = await w3.provider.make_request("tempo_fundAddress", [AsyncWeb3.to_checksum_address(address)])
    if resp.get("error"):
        raise RuntimeError(f"tempo_fundAddress failed: {resp['error']}")
    result = resp.get("result")
    if isinstance(result, list):
        for tx_hash in result:
            await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    return result


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
    """Build an unsigned tempo (``0x76``) tx. Each call is ``{to (None=create), value?, data?}``.

    Set ``awaiting_fee_payer`` when a fee payer will sponsor gas: the sender then
    signs a payload that omits ``fee_token`` (the node recomputes the sender hash
    with ``skip_fee_token`` once a fee-payer signature is present).
    """
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


async def prepare_tx(w3: AsyncWeb3, chain_id: int, sender, calls: Sequence[dict], *, gas_limit: int = STATE_WRITE_GAS):
    """An unsigned tempo tx from ``sender`` over ``calls``, with nonce and fee filled (gas in PATH_USD).

    For custom-signature paths (access keys, keychain) that sign and broadcast separately;
    root-key flows should prefer ``send_calls``/``send_call``.
    """
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=await get_nonce(w3, sender.address),
        fee_token=PATH_USD,
        gas_limit=gas_limit,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=calls,
    )


async def call_revert(w3: AsyncWeb3, to: str, data, *, sender: str | None = None) -> str:
    """eth_call that MUST revert; return the joined error message+data for assertions.

    Tempo precompiles surface the custom error name in the message (e.g. "PolicyForbids")
    and the 4-byte selector in ``data``, so callers can match on either.
    """
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
):
    """Build, sign, and send a tempo tx from ``calls``, filling nonce and fees."""
    sender = Account.from_key(private_key).address
    if nonce is None:
        nonce = await get_nonce(w3, sender)
    priority = DEFAULT_MAX_PRIORITY_FEE_PER_GAS
    tx = build_tempo_tx(
        chain_id=chain_id,
        nonce=nonce,
        fee_token=fee_token,
        gas_limit=gas_limit,
        max_priority_fee_per_gas=priority,
        max_fee_per_gas=await suggested_max_fee(w3, priority),
        calls=calls,
    )
    return await send_tempo_tx(w3, tx, private_key)


async def send_call(w3: AsyncWeb3, chain_id: int, signer, to: str, data, *, gas_limit: int = STATE_WRITE_GAS):
    """Send a single-call tempo tx from ``signer`` (a local account), asserting success."""
    receipt = await send_calls(
        w3, chain_id=chain_id, private_key=signer.key.hex(), gas_limit=gas_limit, calls=[{"to": to, "data": data}]
    )
    assert receipt["status"] == 1
    return receipt


async def latest_timestamp(w3: AsyncWeb3) -> int:
    return (await w3.eth.get_block("latest"))["timestamp"]


def token_from_receipt(receipt, factory: str = TIP20_FACTORY_ADDRESS) -> str:
    """The new token address from the factory's TokenCreated event (indexed topic 1)."""
    log = next(lg for lg in receipt["logs"] if lg["address"].lower() == factory.lower())
    return AsyncWeb3.to_checksum_address(HexBytes(log["topics"][1])[-20:])


async def create_token(w3: AsyncWeb3, *, chain_id: int, admin, quote: str = PATH_USD, name: str = "TUSD", mint=None):
    """Create a TIP-20 via the factory; optionally grant issuer and mint ``(holder, amount)``.

    Returns the new token address (read from the factory's TokenCreated event).
    """
    created = await send_calls(
        w3,
        chain_id=chain_id,
        private_key=admin.key.hex(),
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {
                "to": TIP20_FACTORY_ADDRESS,
                "data": TIP20_FACTORY.fns.createToken(name, name, "USD", quote, admin.address, bytes(32)).data,
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


async def fund_token(
    w3: AsyncWeb3, *, chain_id: int, to: str, token: str, amount: int, funder_pk: str = FAUCET_PRIVATE_KEY
):
    """Transfer ``token`` from the prefunded genesis account, to test paying gas in a non-PATH_USD token."""
    funder = Account.from_key(funder_pk).address
    return await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funder_pk,
        nonce=await get_nonce(w3, funder),
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
    """Mint a FeeAMM pool so gas can be paid in ``user_token``.

    The dev genesis only seeds ``ALPHA_USD``/``PATH_USD``; other stablecoins need
    a pool first. Gas is paid in ``validator_token`` (needs no pool).
    """
    funder = Account.from_key(funder_pk).address
    return await send_calls(
        w3,
        chain_id=chain_id,
        private_key=funder_pk,
        fee_token=validator_token,
        gas_limit=STATE_WRITE_GAS,
        calls=[
            {"to": validator_token, "data": ERC20.fns.approve(FEE_MANAGER_ADDRESS, amount * 4).data},
            {"to": FEE_MANAGER_ADDRESS, "data": FEE.fns.mint(user_token, validator_token, amount, funder).data},
        ],
    )


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
    return await NONCE.fns.getNonce(address, nonce_key).call(w3, to=NONCE_ADDRESS)
