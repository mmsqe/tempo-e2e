"""The default driver: tempo (``tempo node --dev`` + ``tempo-xtask``).

Thin wrapper over the existing ``network`` helpers.
"""

from __future__ import annotations

from pathlib import Path

from web3 import AsyncWeb3

from .. import network
from .base import CAP_CONSENSUS_NET, CAP_FAUCET, CAP_TEMPO_NATIVE


class TempoDriver:
    name = "tempo"

    def capabilities(self) -> set[str]:
        return {CAP_TEMPO_NATIVE, CAP_FAUCET, CAP_CONSENSUS_NET}

    def dev_node(self, base: Path, *, log_name: str = "tempo.log", **kwargs):
        return network.dev_node(base, log_name=log_name, **kwargs)

    async def fund(self, w3: AsyncWeb3, address: str, amount: int) -> object:
        """Fund via the ``tempo_fundAddress`` faucet RPC; await the returned txs."""
        resp = await w3.provider.make_request("tempo_fundAddress", [AsyncWeb3.to_checksum_address(address)])
        if resp.get("error"):
            raise RuntimeError(f"tempo_fundAddress failed: {resp['error']}")
        result = resp.get("result")
        if isinstance(result, list):
            for tx_hash in result:
                await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60.0)
        return result
