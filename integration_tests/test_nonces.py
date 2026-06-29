"""Tempo 2D nonces: parallel keys, sequencing, replay protection, and the Nonce precompile."""

import pytest
from eth_contract.erc20 import ERC20
from tempo import Signer, serialize, sign_transaction
from tempo.constants import NONCE_ADDRESS, PATH_USD

from .abi import NONCE
from .utils import build_tempo_tx, get_nonce, new_account, send_tempo_tx, suggested_max_fee

pytestmark = pytest.mark.tempo


async def _transfer_tx(w3, chain_id, *, nonce, nonce_key, amount=10):
    return build_tempo_tx(
        chain_id=chain_id,
        nonce=nonce,
        nonce_key=nonce_key,
        max_fee_per_gas=await suggested_max_fee(w3),
        calls=[{"to": PATH_USD, "data": ERC20.fns.transfer(new_account().address, amount).data}],
    )


async def test_parallel_nonce_keys_each_start_at_zero(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    for nonce_key in (0, 7, 99):
        tx = await _transfer_tx(w3, chain_id, nonce=0, nonce_key=nonce_key)
        assert (await send_tempo_tx(w3, tx, pk))["status"] == 1, nonce_key


async def test_sequential_nonces_within_a_key(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    for nonce in (0, 1, 2):
        tx = await _transfer_tx(w3, chain_id, nonce=nonce, nonce_key=3)
        assert (await send_tempo_tx(w3, tx, pk))["status"] == 1, nonce


async def test_replayed_nonce_is_rejected(w3, chain_id, funded_account):
    pk = funded_account.key.hex()
    assert (await send_tempo_tx(w3, await _transfer_tx(w3, chain_id, nonce=0, nonce_key=11), pk))["status"] == 1
    replay = sign_transaction(await _transfer_tx(w3, chain_id, nonce=0, nonce_key=11), Signer(pk))
    with pytest.raises(Exception):
        await w3.eth.send_raw_transaction(serialize(replay))


async def test_nonce_precompile_tracks_increments(w3, chain_id, funded_account):
    """getNonce(account, key) on the Nonce precompile starts at 0 and increments per tx."""
    pk, key = funded_account.key.hex(), 42
    assert await NONCE.fns.getNonce(funded_account.address, key).call(w3, to=NONCE_ADDRESS) == 0
    for expected in (1, 2):
        await send_tempo_tx(w3, await _transfer_tx(w3, chain_id, nonce=expected - 1, nonce_key=key), pk)
        assert await NONCE.fns.getNonce(funded_account.address, key).call(w3, to=NONCE_ADDRESS) == expected
    assert await get_nonce(w3, funded_account.address, key) == 2


async def test_nonce_precompile_rejects_protocol_key(w3, funded_account):
    """Nonce key 0 is the protocol nonce, held in account state; the precompile reverts."""
    with pytest.raises(Exception):
        await NONCE.fns.getNonce(funded_account.address, 0).call(w3, to=NONCE_ADDRESS)
