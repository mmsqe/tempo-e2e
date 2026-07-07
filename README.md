# tempo-e2e

End-to-end tests for the **tempo** EVM L1 binary.

## Quickstart

```bash
uv sync
make test                  # boots a local dev node and runs the whole suite
make test-tempo            # only tempo-native feature tests (TIP-20, AA tx, DEX, fees)
make test-consensus        # consensus_* RPC against a 4-validator BFT localnet
make test-consensus-docker # same, but validators run in Docker containers
make lint                  # ruff
```

## Consensus localnet

`make test-consensus` (or `pytest -m consensus --consensus`) launches four
validators that run real Simplex BFT consensus — `tempo-xtask generate-localnet`
writes the genesis and validator keys, and the nodes peer over 127.0.0.1 with
distinct ports (`--consensus.bypass-ip-check`, no loopback aliases needed). It
needs `tempo-xtask` built (`cargo build -p tempo-xtask`). Without `--consensus`
these tests skip, since a `--dev` node does not serve `consensus_*`.

`--consensus-validators N` sets the validator count (default 4, the minimum that
tolerates one fault). The fault-tolerance tests derive how many nodes to stop
from `N` (BFT `N = 3f + 1`) and skip when `N < 4`.

### Docker mode

`make test-consensus-docker` (or `pytest -m consensus --consensus-docker`) runs
the *same* consensus tests with each validator in its own container, via a
generated `docker-compose.yml`. `tempo-xtask` still builds genesis + keys on the
host; the node binary comes from the image (default `tempo:latest`, override with
`TEMPO_IMAGE` / `--tempo-image`). RPC ports are published to the host, so the
tests are backend-agnostic. Skips if `docker` is missing or the image is absent.

The image must match the host `tempo-xtask` version, so pull a matching tag
instead of building — e.g. for a `v1.10.1` checkout:

```bash
docker pull tempoxyz/tempo:1.10.1 && docker tag tempoxyz/tempo:1.10.1 tempo:latest
make test-consensus-docker
```

## Markers

- `tempo` — tempo-native features (TIP-20, AA tx, nonces, fees, DEX)
- `consensus` — needs the multi-validator consensus localnet; skips on a `--dev` node
- `slow` — long-running (e.g. node restart)
