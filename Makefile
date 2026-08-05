.PHONY: install test test-tempo test-consensus test-consensus-docker lint fmt node-up node-down contract-artifacts

BIN := .venv/bin

# App contracts deployed by the suite live in their own repo; their initcode is vendored under
# integration_tests/artifacts so tests need no toolchain. The repo is private, so this uses
# SSH; override CONTRACTS_REPO/CONTRACTS_REF to point elsewhere or pin a commit.
CONTRACTS_REPO ?= git@github.com:NVNM-Chain/nvnm-contracts.git
CONTRACTS_REF ?= main
CONTRACTS_WORK := .cache/nvnm-contracts

install:
	uv sync

# Rebuild the vendored deployer initcode from the contracts repo. Needs forge and jq.
contract-artifacts:
	rm -rf $(CONTRACTS_WORK)
	# init+fetch rather than clone --branch so CONTRACTS_REF may be a branch, tag, or SHA.
	git init -q $(CONTRACTS_WORK)
	cd $(CONTRACTS_WORK) && git remote add origin $(CONTRACTS_REPO) && \
	  git fetch -q --depth 1 origin $(CONTRACTS_REF) && git checkout -q FETCH_HEAD && \
	  git submodule update -q --init --depth 1
	cd $(CONTRACTS_WORK) && forge build
	jq -n \
	  --arg bc "$$(jq -r '.bytecode.object' $(CONTRACTS_WORK)/out/AnchoringDeployer.sol/AnchoringDeployer.json)" \
	  --arg repo "$(CONTRACTS_REPO)" \
	  --arg commit "$$(git -C $(CONTRACTS_WORK) rev-parse HEAD)" \
	  '{source: $$repo, commit: $$commit, note: "Regenerate with: make contract-artifacts", deployer_bytecode: $$bc}' \
	  > integration_tests/artifacts/anchoring.json
	@echo "wrote integration_tests/artifacts/anchoring.json (nvnm-contracts $$(git -C $(CONTRACTS_WORK) rev-parse --short HEAD))"

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
