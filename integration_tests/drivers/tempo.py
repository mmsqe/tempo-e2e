"""The default driver: tempo (``tempo node --dev`` + ``tempo-xtask``).

Thin wrapper over the existing ``network`` helpers.
"""

from __future__ import annotations

from pathlib import Path

from web3 import AsyncWeb3

from .. import network
from ..utils import fund
from .base import CAP_CONSENSUS_NET, CAP_FAUCET, CAP_INDEXER_RPC, CAP_TEMPO_NATIVE


class TempoDriver:
    name = "tempo"

    def capabilities(self) -> set[str]:
        # The indexer namespaces are registered but every handler is a stub, so only
        # the wire-contract cap; CAP_INDEXER comes from --indexer/--tidx.
        return {CAP_TEMPO_NATIVE, CAP_FAUCET, CAP_CONSENSUS_NET, CAP_INDEXER_RPC}

    def dev_node(self, base: Path, *, log_name: str = "tempo.log", **kwargs):
        return network.dev_node(base, log_name=log_name, **kwargs)

    async def send_tx(self, w3: AsyncWeb3, chain_id: int, sender) -> dict:
        """A PATH_USD transfer as tempo's native AA transaction (type 0x76)."""
        from ..utils import new_account, send_calls, transfer_call

        return await send_calls(
            w3,
            chain_id=chain_id,
            private_key=sender.key.hex(),
            calls=[transfer_call(new_account().address, 1)],
        )

    async def fund(self, w3: AsyncWeb3, address: str, amount: int) -> object:
        """The faucet RPC, or a transfer on a network that has none. How much is the faucet's to
        choose, and the suite's own when it transfers, so the caller's amount goes unused."""
        return await fund(w3, address)
