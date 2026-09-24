.PHONY: install test test-tempo test-consensus test-consensus-docker lint fmt node-up node-down contract-artifacts hyperlane-artifacts

BIN := .venv/bin

# The app contracts the staking suites deploy (NVNMStaking, FeeRouter, …) are built from the
# `contracts` submodule, and their initcode is vendored under integration_tests/artifacts so the
# tests themselves need no toolchain. The submodule is already the checkout main reads
# `contracts/layout/` from, so regenerating pins to whatever commit it points at.
CONTRACTS_WORK := contracts
# The bridge contracts the round-trip suite deploys on both chains, from their own repo.
BRIDGE_WORK := bridge

# _artifact,<submodule>,<source .sol>,<contract>,<output json> — vendor one contract's initcode
# and where it came from. The repo is read off the submodule, so a second source needs no second
# macro and cannot be labelled with the wrong origin.
define _artifact
	jq -n \
	  --arg bc "$$(jq -r '.bytecode.object' $(1)/out/$(2).sol/$(3).json)" \
	  --arg repo "$$(git -C $(1) remote get-url origin)" \
	  --arg commit "$$(git -C $(1) rev-parse HEAD)" \
	  '{source:$$repo, commit:$$commit, note:"Regenerate with: make contract-artifacts", deployer_bytecode:$$bc}' \
	  > integration_tests/artifacts/$(4)
	@echo "wrote integration_tests/artifacts/$(4) ($(1) $$(git -C $(1) rev-parse --short HEAD))"
endef

install:
	uv sync

# Rebuild the vendored artifacts from the submodule. Needs forge and jq. Update the submodule
# first if you want a newer contracts commit than the one it is pinned to.
contract-artifacts:
	git submodule update --init --recursive $(CONTRACTS_WORK) $(BRIDGE_WORK)
	cd $(CONTRACTS_WORK) && forge build
	cd $(BRIDGE_WORK) && forge build
	$(call _artifact,$(CONTRACTS_WORK),StakingDeployer,StakingDeployer,staking.json)
	$(call _artifact,$(CONTRACTS_WORK),FeeRouter,FeeRouterFactory,feerouter_factory.json)
	$(call _artifact,$(CONTRACTS_WORK),FeeRouter,FeeRouter,feerouter.json)
	$(call _artifact,$(CONTRACTS_WORK),MockSwapPool,MockSwapPool,swap_pool.json)
	$(call _artifact,$(CONTRACTS_WORK),MockERC20,MockERC20,mock_erc20.json)
	$(call _artifact,$(CONTRACTS_WORK),BridgedNVNM,BridgedNVNM,bridged_nvnm.json)
	$(call _artifact,$(CONTRACTS_WORK),GuardedSwapper,GuardedSwapper,guarded_swapper.json)
	$(call _artifact,$(BRIDGE_WORK),NVNMLockbox,NVNMLockbox,lockbox.json)
	$(call _artifact,$(BRIDGE_WORK),NVNMBridgeAdapter,NVNMBridgeAdapter,bridge_adapter.json)
	$(call _artifact,$(BRIDGE_WORK),NVNMReleaseAdapter,NVNMReleaseAdapter,release_adapter.json)

# Hyperlane's contracts, as its npm release publishes them. Needs node and npm.
HYPERLANE_CORE := 12.1.0
hyperlane-artifacts:
	@tmp=$$(mktemp -d) && cd $$tmp && npm pack -q @hyperlane-xyz/core@$(HYPERLANE_CORE) >/dev/null && \
	  tar xzf *.tgz && node $(CURDIR)/scripts/hyperlane-artifacts.js $$tmp/package $(HYPERLANE_CORE) \
	  > $(CURDIR)/integration_tests/artifacts/hyperlane.json && rm -rf $$tmp
	@echo "wrote integration_tests/artifacts/hyperlane.json (@hyperlane-xyz/core $(HYPERLANE_CORE))"

# Full suite (launches a local dev node).
test:
	$(BIN)/pytest -vv

# Only tempo-native feature tests.
test-tempo:
	$(BIN)/pytest -m tempo -vv

# Consensus RPC tests against a 4-validator localnet (needs tempo-xtask built).
test-consensus:
	$(BIN)/pytest -m consensus --consensus -vv

# Same consensus tests, but the validators run in Docker (needs tempo-xtask built
# on the host and a `tempo:latest` image; override with TEMPO_IMAGE=...).
test-consensus-docker:
	$(BIN)/pytest -m consensus --consensus-docker -vv

lint:
	$(BIN)/ruff check integration_tests

fmt:
	$(BIN)/ruff format integration_tests

# Launch / stop a standalone dev node (uses the same flags as the test harness).
node-up:
	$(BIN)/python -m integration_tests.devnode up

node-down:
	$(BIN)/python -m integration_tests.devnode down
