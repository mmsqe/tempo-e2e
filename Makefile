.PHONY: install test test-tempo test-consensus test-consensus-docker lint fmt node-up node-down contract-artifacts

BIN := .venv/bin
# The app contracts (AnchoringRegistry, NVNMStaking) live in the tempo repo; fetch + build from git.
TEMPO_REPO ?= https://github.com/mmsqe/tempo
TEMPO_REF ?= nvm
TEMPO_WORK := .cache/tempo

# _artifact,<Deployer contract>,<output json> — vendor one deployer's initcode + provenance.
define _artifact
	jq -n \
	  --arg bc "$$(jq -r '.bytecode.object' $(TEMPO_WORK)/contracts/out/$(1).sol/$(1).json)" \
	  --arg repo "$(TEMPO_REPO)" --arg ref "$(TEMPO_REF)" --arg commit "$$(git -C $(TEMPO_WORK) rev-parse HEAD)" \
	  '{source:$$repo, ref:$$ref, commit:$$commit, note:"Regenerate with: make contract-artifacts", deployer_bytecode:$$bc}' \
	  > integration_tests/artifacts/$(2)
	@echo "wrote integration_tests/artifacts/$(2) (tempo $$(git -C $(TEMPO_WORK) rev-parse --short HEAD))"
endef

install:
	uv sync

# Rebuild the vendored deployer initcodes from the tempo repo's Foundry sources.
contract-artifacts:
	rm -rf $(TEMPO_WORK)
	# init+fetch instead of clone --branch so TEMPO_REF may be a branch, tag, or commit SHA.
	git init -q $(TEMPO_WORK)
	cd $(TEMPO_WORK) && git remote add origin $(TEMPO_REPO) && \
	  git fetch -q --depth 1 origin $(TEMPO_REF) && git checkout -q FETCH_HEAD && \
	  git submodule update --init --depth 1 contracts/lib/solady contracts/lib/forge-std
	cd $(TEMPO_WORK)/contracts && forge build
	$(call _artifact,AnchoringDeployer,anchoring.json)
	$(call _artifact,StakingDeployer,staking.json)
	$(call _artifact,FeeRouter,feerouter.json)

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
