.PHONY: install test test-tempo test-consensus lint fmt node-up node-down

BIN := .venv/bin

install:
	uv sync

# Full suite (launches a local dev node).
test:
	$(BIN)/pytest -vv

# Only tempo-native feature tests.
test-tempo:
	$(BIN)/pytest -m tempo -vv

# Consensus RPC tests against a 4-validator localnet (needs tempo-xtask built).
test-consensus:
	$(BIN)/pytest -m consensus --consensus -vv

lint:
	$(BIN)/ruff check integration_tests

fmt:
	$(BIN)/ruff format integration_tests

# Launch / stop a standalone dev node (uses the same flags as the test harness).
node-up:
	$(BIN)/python -m integration_tests.devnode up

node-down:
	$(BIN)/python -m integration_tests.devnode down
