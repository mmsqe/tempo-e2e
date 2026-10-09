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

# LayerZero's own contracts, from the npm release the bridge pins.
LZ_NPM := bridge/layerzero/node_modules/@layerzerolabs
LZ_PROTOCOL := $(LZ_NPM)/lz-evm-protocol-v2/artifacts/contracts
LZ_ULN := $(LZ_NPM)/lz-evm-messagelib-v2/artifacts/contracts/uln/uln302

# Vendor the initcode the suites deploy, from the submodules at their pins, so the tests need no
# toolchain. Needs forge, jq and npm.
contract-artifacts:
	git submodule update --init --recursive contracts bridge erc20
	cd contracts && forge build
	cd bridge && forge build
	cd bridge/layerzero && npm ci --silent && forge build
	cd erc20 && forge build
	$(call _artifact,contracts,StakingDeployer,StakingDeployer,staking.json)
	$(call _artifact,contracts,FeeRouter,FeeRouterFactory,feerouter_factory.json)
	$(call _artifact,contracts,FeeRouter,FeeRouter,feerouter.json)
	$(call _artifact,contracts,FeeLockbox,FeeLockbox,fee_lockbox.json)
	$(call _artifact,contracts,MockSwapPool,MockSwapPool,swap_pool.json)
	$(call _artifact,contracts,BridgedNVNM,BridgedNVNM,bridged_nvnm.json)
	$(call _artifact,contracts,GuardedSwapper,GuardedSwapper,guarded_swapper.json)
	$(call _artifact,bridge,NVNMLockbox,NVNMLockbox,lockbox.json)
	$(call _artifact,bridge,BridgedTIP20,BridgedTIP20,bridged_tip20.json)
	$(call _artifact,bridge/layerzero,NVNMLockGateway,NVNMLockGateway,lz_lock_gateway.json)
	$(call _artifact,bridge/layerzero,NVNMMintGateway,NVNMMintGateway,lz_mint_gateway.json)
	$(call _artifact,bridge/layerzero,MockWorker,MockWorker,lz_worker.json)
	$(call _artifact,erc20,NVNMToken,NVNMToken,nvnm_token.json)
	$(call _artifact,erc20,ERC1967Proxy,ERC1967Proxy,erc1967_proxy.json)
	jq -n \
	  --arg v "$$(jq -r .version $(LZ_NPM)/lz-evm-protocol-v2/package.json)" \
	  --slurpfile e $(LZ_PROTOCOL)/EndpointV2.sol/EndpointV2.json \
	  --slurpfile alt $(LZ_PROTOCOL)/EndpointV2Alt.sol/EndpointV2Alt.json \
	  --slurpfile send $(LZ_ULN)/SendUln302.sol/SendUln302.json \
	  --slurpfile recv $(LZ_ULN)/ReceiveUln302.sol/ReceiveUln302.json \
	  '{source:"npm:@layerzerolabs/lz-evm-{protocol,messagelib}-v2@\($$v)", note:"Regenerate with: make contract-artifacts", \
	    deployer_bytecode:{EndpointV2:$$e[0].bytecode, EndpointV2Alt:$$alt[0].bytecode, \
	      SendUln302:$$send[0].bytecode, ReceiveUln302:$$recv[0].bytecode}}' \
	  > integration_tests/artifacts/layerzero.json
	@echo "wrote integration_tests/artifacts/layerzero.json"

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
