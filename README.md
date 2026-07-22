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

The datadir is kept after each run (pass `--clean-data` to wipe it). Every node
keeps its chain data and a live `node.log` you can `tail -f` mid-run.

### Docker mode

`make test-consensus-docker` (or `pytest -m consensus --consensus-docker`) runs
the *same* consensus tests with each validator in its own container, via a
generated `docker-compose.yml`. `tempo-xtask` still builds genesis + keys on the
host; the node binary comes from the image (default `tempo:latest`, override with
`TEMPO_IMAGE` / `--tempo-image`). RPC ports are published to the host, so the
tests are backend-agnostic. Skips if `docker` is missing or the image is absent.

The image must match the host `tempo-xtask` version. Pull it from
[ghcr.io/tempoxyz/tempo](https://github.com/tempoxyz/tempo/pkgs/container/tempo)
instead of building — `latest` tracks the newest `main` build, so it pairs with a
`tempo-xtask` built from an up-to-date checkout:

```bash
docker pull ghcr.io/tempoxyz/tempo:latest && docker tag ghcr.io/tempoxyz/tempo:latest tempo:latest
make test-consensus-docker
```

If your `tempo-xtask` comes from an older checkout, pull that release's tag
(e.g. `ghcr.io/tempoxyz/tempo:1.10.1`) rather than `latest`.

## Markers

- `tempo` — tempo-native features (TIP-20, AA tx, nonces, fees, DEX)
- `consensus` — needs the multi-validator consensus localnet; skips on a `--dev` node
- `slow` — long-running (e.g. node restart)

## Test map

### Liveness & core EVM/RPC

| File | TIP(s) | Covers |
|---|---|---|
| `test_smoke.py` | — | node connected, chain started, blocks advance |
| `test_chain_id.py` | — | `eth_chainId` / `net_version` consistency |
| `test_rpc.py` | TIP-1031 | client version, not-syncing, tx-by-hash (0x76 AA), block receipts, state override |
| `test_eip1559.py` | EIP-1559 | base fee present, effective price ≤ maxFee, maxFee-below-basefee rejection |
| `test_fee_history.py` | — | `eth_feeHistory` shape and reward percentiles |
| `test_filters.py` | — | event logs / filters: transfer log, `get_logs`, block & log filters |
| `test_subscribe.py` | — | `eth_subscribe`: new heads, logs |
| `test_tracing.py` | — | `debug_traceTransaction` (callTracer, struct logger), `trace_block_by_number` |
| `test_contract.py` | — | EVM contract deploy + call via tempo (0x76) txs |
| `test_native_token.py` | — | `BALANCE` opcode is 0 for a stablecoin-funded account |
| `test_mempool.py` | — | `txpool_status`/`content` and `operator_peers` RPCs |
| `test_faucet.py` | — | `tempo_fundAddress` faucet RPC |
| `test_validation.py` | — | malformed / unfunded tx rejection |
| `test_gas.py` | TIP-1000, TIP-1010 | gas estimation/accounting: state-creation cost, 30M per-tx cap |
| `test_node_ops.py` | — | node resumes from persisted state on restart (`slow`) |

### Tempo native tx & account abstraction

| File | TIP(s) | Covers |
|---|---|---|
| `test_tempo_tx.py` | — | native 0x76 tx: type, heterogeneous batching under one nonce, validity windows |
| `test_standard_tx.py` | TIP-1060 | unmodified type-2 tx pays gas in the default stablecoin |
| `test_nonces.py` | — | 2D nonces: parallel keys, sequencing, replay, Nonce precompile |
| `test_expiring_nonce.py` | TIP-1009 | expiring nonces: success, expiry, replay, max-window, zero-nonce |
| `test_7702.py` | EIP-7702 | set-code delegation: install, execute, revoke |
| `test_access_keys.py` | — | delegated admin access key signs a tempo tx |
| `test_access_key_scopes.py` | TIP-1011 | scoped access keys: call scoping, spend caps, period resets |
| `test_permit.py` | TIP-1004 | EIP-2612 permit: 712 approval, domain, expiry/wrong-signer/replay reverts |
| `test_keychain_admin.py` | TIP-1049, TIP-1053 | AccountKeychain admin keys + witnesses (T5/T6) |
| `test_signature_precompile.py` | TIP-1020 | SignatureVerifier precompile: recover/verify, admin/expired-key cases |

### TIP-20 stablecoin & addressing

| File | TIP(s) | Covers |
|---|---|---|
| `test_tip20.py` | TIP-20 | transfer, approve/transferFrom, batches, transferWithMemo |
| `test_tip20_factory.py` | TIP-20 | factory: createToken, ISSUER_ROLE, mint, burn |
| `test_virtual_address.py` | TIP-1022, TIP-1035 | virtual addresses (T3+): deposit/mint/transferFrom forward to master |
| `test_channel_reserve.py` | TIP-1034, TIP-1035 | payment-channel reserve (T5): lock, voucher settle, close/refund |

### Policies

| File | TIP(s) | Covers |
|---|---|---|
| `test_policy.py` | TIP-403, TIP-1022 | transfer policy whitelist/blacklist; virtual address rejected as member |
| `test_compound_policy.py` | TIP-1015, TIP-403 | compound policies compose sender/recipient/mint sub-policies by role (T2+) |
| `test_receive_policy.py` | TIP-1028, TIP-403 | receive policies + guard (T6): escrow/claim/recovery, token/sender filters |

### Fees, gas market, storage credits & rewards

| File | TIP(s) | Covers |
|---|---|---|
| `test_fee_amm.py` | — | gas paid in a non-validator stablecoin via the FeeManager AMM pool, no-pool token rejected naming the fee token |
| `test_fee_routing.py` | TIP-1033 | two-hop FeeAMM routing X→ALPHA→PATH; direct-pool preference; m² haircut |
| `test_fee_token.py` | — | pay gas in a chosen stablecoin via the `fee_token` field (FeeAMM) |
| `test_fee_sponsor.py` | — | a fee payer covers gas for another account; unfunded payer rejected |
| `test_fee_manager_policy.py` | TIP-1042 | FeeManager policy exemptions (T8+): blacklist still pays gas; mint/burn auth |
| `test_dynamic_basefee.py` | TIP-1067 | dynamic base fee: congestion raises, idle decays (EIP-1559 integer formula) |
| `test_storage_credits.py` | TIP-1060, TIP-1064 | storage credits (T7): slot-delete mint, recreate refund, replace consume |
| `test_payment_lane.py` | TIP-1045 | payment-lane classification (T5): general lane capped, TIP-20 payments not |
| `test_rewards_deprecation.py` | TIP-1075 | TIP-20 rewards deprecation (T8 removes machinery): opt-in/distribute noop, no accrual |

### DEX

| File | TIP(s) | Covers |
|---|---|---|
| `test_dex.py` | TIP-1030, TIP-1056, TIP-1087 | stablecoin DEX against PATH_USD: limit orders & swaps, same-tick flip orders (flip-in-place, cancel refund; T5+), book index (T8+) |

### Precompiles & on-chain config

| File | TIP(s) | Covers |
|---|---|---|
| `test_precompiles.py` | — | a contract STATICCALLs each enshrined precompile for deterministic output |
| `test_current_committee.py` | TIP-1070 | current-committee precompile (T8+): read members, system-only writes, epoch boundary |
| `test_validator_config.py` | TIP-1017 | ValidatorConfig V2 append-only registry: genesis state, owner-gated mutators |

### Consensus & networking (`consensus` marker)

| File | TIP(s) | Covers |
|---|---|---|
| `test_consensus_rpc.py` | TIP-1031 | `consensus_*` RPC on a multi-validator localnet; restart / quorum recovery |
| `test_consensus_storage.py` | — | SIGKILL/unclean-kill recovery, consensus-storage wipe rejoin, strict-startup (`slow`) |
| `test_public_node.py` | — | validators→follower→public→proxy: sync, same-fork, devp2p peering, tx gossip |
