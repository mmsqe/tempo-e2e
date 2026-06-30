"""Shared helpers: tempo-py signs the ``0x76`` tx, ``eth_contract`` builds calldata, ``AsyncWeb3`` transports."""

from __future__ import annotations

from typing import Sequence

from eth_account import Account
from eth_contract.erc20 import ERC20
from hexbytes import HexBytes
from tempo import Builder, Signer, serialize, sign_transaction
from tempo.constants import ALPHA_USD, BETA_USD, NONCE_ADDRESS, PATH_USD, THETA_USD
from web3 import AsyncWeb3

from .abi import NONCE
from .network import FAUCET_PRIVATE_KEY

# The four enshrined TIP-20 stablecoins, by symbol.
STABLECOINS = {"PATH_USD": PATH_USD, "ALPHA_USD": ALPHA_USD, "BETA_USD": BETA_USD, "THETA_USD": THETA_USD}

DEFAULT_GAS_LIMIT = 2_000_000
DEFAULT_MAX_PRIORITY_FEE_PER_GAS = 2_000_000_000
DEFAULT_MAX_FEE_PER_GAS = 100_000_000_000
# A tempo tx that writes new storage (DEX orders, token deploys) needs extra TIP-1060 state gas.
STATE_WRITE_GAS = 8_000_000

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
):
    """Build an unsigned tempo (``0x76``) tx. Each call is ``{to (None=create), value?, data?}``."""
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
    if valid_before is not None:
        builder.valid_before(valid_before)
    if valid_after is not None:
        builder.valid_after(valid_after)
    for call in calls:
        builder.add_call(to=call.get("to") or "", value=call.get("value", 0), data=call.get("data", b""))
    return builder.build()


async def send_tempo_tx(w3: AsyncWeb3, tx, private_key: str, timeout: float = 60.0):
    """Sign, broadcast, and await the receipt for a tempo transaction."""
    raw = serialize(sign_transaction(tx, Signer(private_key)))
    return await w3.eth.wait_for_transaction_receipt(await w3.eth.send_raw_transaction(raw), timeout=timeout)


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
