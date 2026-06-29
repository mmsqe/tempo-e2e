# tempo-e2e

End-to-end tests for the **tempo** EVM L1 binary.

## Quickstart

```bash
uv sync
make test            # boots a local dev node and runs the whole suite
make test-tempo      # only tempo-native feature tests (TIP-20, AA tx, DEX, fees)
make test-consensus  # consensus_* RPC against a 4-validator BFT localnet
make lint            # ruff
```

## Consensus localnet

`make test-consensus` (or `pytest -m consensus --consensus`) launches four
validators that run real Simplex BFT consensus — `tempo-xtask generate-localnet`
writes the genesis and validator keys, and the nodes peer over 127.0.0.1 with
distinct ports (`--consensus.bypass-ip-check`, no loopback aliases needed). It
needs `tempo-xtask` built (`cargo build -p tempo-xtask`). Without `--consensus`
these tests skip, since a `--dev` node does not serve `consensus_*`.

## Markers

- `tempo` — tempo-native features (TIP-20, AA tx, nonces, fees, DEX)
- `consensus` — needs the multi-validator consensus localnet; skips on a `--dev` node
- `slow` — long-running (e.g. node restart)
