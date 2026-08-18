.PHONY: install test test-tempo test-consensus test-consensus-docker lint fmt node-up node-down contract-artifacts

BIN := .venv/bin

# App contracts deployed by the suite (Registry, NVNMStaking, FeeRouter, …) live in their own
# repo; what the tests need of them is vendored under integration_tests/artifacts so tests need
# no toolchain. The repo is private, so this uses SSH; override CONTRACTS_REPO/CONTRACTS_REF to
# point elsewhere or pin a commit.
CONTRACTS_REPO ?= git@github.com:NVNM-Chain/nvnmchain-contracts.git
CONTRACTS_REF ?= staking
CONTRACTS_WORK := .cache/nvnmchain-contracts
ARTIFACT := integration_tests/artifacts/registry.json

# Where to fetch from, as opposed to what the artifact records. Defaults to the same place;
# point it at a local checkout to regenerate against a commit that is not pushed yet:
#
#     make contract-artifacts CONTRACTS_ORIGIN=../nvnmchain-contracts
#
# Provenance still names CONTRACTS_REPO, because a path on one machine tells a later reader
# nothing. That leaves CONTRACTS_REF the thing to check: until it is pushed, the artifact
# records a commit no one else can fetch.
CONTRACTS_ORIGIN ?= $(CONTRACTS_REPO)

# The RecordCategory enum in declaration order. The ABI encodes it as a bare uint8, so the
# names live only in the source; vendoring them beside the initcode they were built from is
# what lets the suite derive its mapping instead of mirroring it by hand.
CATEGORIES_AWK := /enum RecordCategory \{/{f=1;next} f&&/\}/{exit} f{sub(/\/\/.*/,"");gsub(/[ ,]/,"");if($$0!="")print}

# _artifact,<source .sol>,<contract>,<output json> — vendor one contract's initcode + provenance.
define _artifact
	jq -n \
	  --arg bc "$$(jq -r '.bytecode.object' $(CONTRACTS_WORK)/out/$(1).sol/$(2).json)" \
	  --arg repo "$(CONTRACTS_REPO)" \
	  --arg commit "$$(git -C $(CONTRACTS_WORK) rev-parse HEAD)" \
	  '{source:$$repo, commit:$$commit, note:"Regenerate with: make contract-artifacts", deployer_bytecode:$$bc}' \
	  > integration_tests/artifacts/$(3)
	@echo "wrote integration_tests/artifacts/$(3) (nvnmchain-contracts $$(git -C $(CONTRACTS_WORK) rev-parse --short HEAD))"
endef

install:
	uv sync

# Rebuild the vendored artifact from the contracts repo. Needs forge and jq.
contract-artifacts:
	rm -rf $(CONTRACTS_WORK)
	# init+fetch rather than clone --branch so CONTRACTS_REF may be a branch, tag, or SHA.
	git init -q $(CONTRACTS_WORK)
	cd $(CONTRACTS_WORK) && git remote add origin $(CONTRACTS_ORIGIN) && \
	  git fetch -q --depth 1 origin $(CONTRACTS_REF) && git checkout -q FETCH_HEAD && \
	  git submodule update -q --init --depth 1
	cd $(CONTRACTS_WORK) && forge build
	# Its own recipe rather than `_artifact`: the registry artifact carries the RecordCategory
	# enum beside the initcode, so the bytecode and the enum can only come from one build.
	jq -n \
	  --arg repo "$(CONTRACTS_REPO)" \
	  --arg commit "$$(git -C $(CONTRACTS_WORK) rev-parse HEAD)" \
	  --arg bc "$$(jq -r '.bytecode.object' $(CONTRACTS_WORK)/out/RegistryDeployer.sol/RegistryDeployer.json)" \
	  --argjson categories "$$(awk '$(CATEGORIES_AWK)' $(CONTRACTS_WORK)/src/Registry.sol | jq -R . \
	    | jq -s 'if length > 0 then . else error("no RecordCategory members in Registry.sol") end')" \
	  '{source: $$repo, commit: $$commit, note: "Regenerate with: make contract-artifacts", record_categories: $$categories, deployer_bytecode: $$bc}' \
	  > $(ARTIFACT)
	@echo "wrote $(ARTIFACT) (nvnmchain-contracts $$(git -C $(CONTRACTS_WORK) rev-parse --short HEAD), $$(jq -r '.record_categories|length' $(ARTIFACT)) categories)"
	$(call _artifact,StakingDeployer,StakingDeployer,staking.json)
	$(call _artifact,FeeRouter,FeeRouterFactory,feerouter_factory.json)
	$(call _artifact,FeeRouter,FeeRouter,feerouter.json)
	$(call _artifact,MockSwapPool,MockSwapPool,swap_pool.json)
	$(call _artifact,MockERC20,MockERC20,mock_erc20.json)
	$(call _artifact,BridgedNVNM,BridgedNVNM,bridged_nvnm.json)
	$(call _artifact,GuardedSwapper,GuardedSwapper,guarded_swapper.json)

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
