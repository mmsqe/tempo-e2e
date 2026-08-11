"""Backend driver interface for the e2e suite.

A *driver* encapsulates what is specific to one fork binary; the rest of the
suite is plain Ethereum JSON-RPC. Drivers are duck-typed (see ``tempo.py`` and
``allegro.py``) and provide::

    name                                   # backend name shown in skips
    capabilities() -> set[str]             # CAP_* tokens the fork supports
    dev_node(base, *, log_name=..., **kw)  # single node, ready to start()
    async fund(w3, address, amount)        # make address able to pay gas
    async send_tx(w3, chain_id, sender)    # one ordinary tx from sender -> receipt

Nodes expose ``start()`` / ``wait_for_rpc()`` / ``stop()``, ``rpc_url`` /
``ws_url``, and ``chain_id``. A test that needs a fork-specific feature
declares ``@pytest.mark.requires("<cap>")`` (or the legacy ``tempo`` /
``consensus`` marks) and is skipped when the active backend lacks the cap.
"""

CAP_TEMPO_NATIVE = "tempo-native"  # native/AA (0x76) txs, fee tokens, tempo precompiles
CAP_FAUCET = "faucet"  # tempo_fundAddress faucet RPC
CAP_CONSENSUS_NET = "consensus-net"  # multi-validator consensus localnet (tempo-devnet)
CAP_EMBEDDED_VALIDATORS = "embedded-validators"  # validators embedded in genesis.json + make_cluster()
CAP_INDEXER_RPC = "indexer-rpc"  # node registers eth_getTransactions / token_* (handlers may be stubs)
# …and a live indexer answers them with chain data. For a sidecar that is a property of
# the deployment rather than the binary, so conftest adds it from --indexer. For a
# backend that indexes inside the node (allegro's reth ExEx) it *is* a property of the
# binary, and that driver advertises it directly -- otherwise the semantic tier would
# depend on someone remembering a flag. Either source grants it; they are not exclusive.
CAP_INDEXER = "indexer"
