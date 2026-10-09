.PHONY: install test test-tempo test-consensus test-consensus-docker lint fmt node-up node-down contract-artifacts

BIN := .venv/bin

# _artifact,<submodule>,<source .sol>,<contract>,<output json>: one contract's initcode, and the
# repo and commit it was built from.
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

# Vendor the initcode the suites deploy, from the submodule at its pin, so the tests need no
# toolchain. Needs forge and jq.
contract-artifacts:
	git submodule update --init --recursive contracts
	cd contracts && forge build
	$(call _artifact,contracts,StakingDeployer,StakingDeployer,staking.json)
	$(call _artifact,contracts,FeeRouter,FeeRouterFactory,feerouter_factory.json)
	$(call _artifact,contracts,FeeRouter,FeeRouter,feerouter.json)
	$(call _artifact,contracts,FeeLockbox,FeeLockbox,fee_lockbox.json)
	$(call _artifact,contracts,MockSwapPool,MockSwapPool,swap_pool.json)
	$(call _artifact,contracts,BridgedNVNM,BridgedNVNM,bridged_nvnm.json)
	$(call _artifact,contracts,GuardedSwapper,GuardedSwapper,guarded_swapper.json)

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
