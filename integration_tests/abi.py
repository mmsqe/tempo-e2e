"""ABIs for tempo precompiles. Standard token ops reuse ``eth_contract.erc20.ERC20``."""

from eth_contract import Contract

# 2D nonce precompile (INonce). Nonce key 0 is the protocol nonce and reverts here.
NONCE = Contract.from_abi(["function getNonce(address account, uint256 nonceKey) view returns (uint64)"])

# Stablecoin DEX precompile (IStablecoinDEX): order book keyed against PATH_USD.
DEX = Contract.from_abi(
    [
        "function place(address token, uint128 amount, bool isBid, int16 tick) returns (uint128 orderId)",
        "function cancel(uint128 orderId)",
        "function createPair(address base) returns (bytes32 key)",
        "function swapExactAmountIn(address tokenIn, address tokenOut, uint128 amountIn, uint128 minAmountOut) returns (uint128 amountOut)",
        "function quoteSwapExactAmountIn(address tokenIn, address tokenOut, uint128 amountIn) view returns (uint128 amountOut)",
        "function getOrder(uint128 orderId) view returns ((uint128 orderId, address maker, bytes32 key, bool isBid, int16 tick, uint128 amount, uint128 remaining, uint128 prev, uint128 next, bool isFlip, int16 flipTick))",
        "function nextOrderId() view returns (uint128)",
        "function pairKey(address a, address b) pure returns (bytes32)",
        "function tickToPrice(int16 tick) pure returns (uint32)",
        "function priceToTick(uint32 price) pure returns (int16)",
        "function MIN_ORDER_AMOUNT() pure returns (uint128)",
    ]
)

# Fee manager / fee AMM precompile (IFeeManager + IFeeAMM).
FEE = Contract.from_abi(
    [
        "function setUserToken(address token)",
        "function userTokens(address user) view returns (address)",
        "function validatorTokens(address validator) view returns (address)",
        "function getPool(address userToken, address validatorToken) view returns ((uint128 reserveUserToken, uint128 reserveValidatorToken))",
        "function getPoolId(address userToken, address validatorToken) pure returns (bytes32)",
        "function mint(address userToken, address validatorToken, uint256 amountValidatorToken, address to) returns (uint256 liquidity)",
        "function burn(address userToken, address validatorToken, uint256 liquidity, address to) returns (uint256 amountUserToken, uint256 amountValidatorToken)",
        "function liquidityBalances(bytes32 poolId, address user) view returns (uint256)",
    ]
)

# TIP-20 token factory (ITIP20Factory) and AccessControl on the created tokens.
TIP20_FACTORY = Contract.from_abi(
    [
        "function createToken(string name, string symbol, string currency, address quoteToken, address admin, bytes32 salt) returns (address)",
        "function isTIP20(address token) view returns (bool)",
    ]
)
TIP20_ROLES = Contract.from_abi(["function grantRole(bytes32 role, address account)"])

# Tempo TIP-20 extensions beyond ERC-20 (standard ops use eth_contract.erc20.ERC20).
TIP20 = Contract.from_abi(
    [
        "function transferWithMemo(address to, uint256 amount, bytes32 memo)",
        "function burn(uint256 amount)",
    ]
)

# TIP-403 transfer-policy registry (ITIP403Registry); PolicyType: WHITELIST=0, BLACKLIST=1, COMPOUND=2.
TIP403 = Contract.from_abi(
    [
        "function createPolicy(address admin, uint8 policyType) returns (uint64)",
        "function modifyPolicyWhitelist(uint64 policyId, address account, bool allowed)",
        "function modifyPolicyBlacklist(uint64 policyId, address account, bool restricted)",
        "function isAuthorized(uint64 policyId, address user) view returns (bool)",
        "function policyIdCounter() view returns (uint64)",
        "function policyData(uint64 policyId) view returns (uint8 policyType, address admin)",
    ]
)

# Validator config precompiles (IValidatorConfig / IValidatorConfigV2); validatorCount is common.
VALIDATOR_CONFIG = Contract.from_abi(["function validatorCount() view returns (uint64)"])

# Address registry precompile (T3+): virtual-address helpers.
ADDRESS_REGISTRY = Contract.from_abi(["function isVirtualAddress(address addr) pure returns (bool)"])

# Storage credits precompile (T7+).
STORAGE_CREDITS = Contract.from_abi(["function balanceOf(address account) view returns (uint64)"])

# TIP-20 payment-channel reserve precompile (TIP-1034, T5+). `descriptor` is the
# 7-field channel identity; `expiringNonceHash` is assigned at open (read from the
# ChannelOpened event). settle's signature is an EIP-712 voucher over getVoucherDigest.
_CR_DESC = (
    "(address payer,address payee,address operator,address token,bytes32 salt,"
    "address authorizedSigner,bytes32 expiringNonceHash)"
)
_CR_STATE = "(uint96 settled,uint96 deposit,uint32 closeRequestedAt)"
TIP20_CHANNEL_RESERVE = Contract.from_abi(
    [
        "function CLOSE_GRACE_PERIOD() view returns (uint64)",
        "function domainSeparator() view returns (bytes32)",
        "function open(address payee, address operator, address token, uint96 deposit, bytes32 salt, address authorizedSigner) returns (bytes32 channelId)",
        f"function topUp({_CR_DESC} descriptor, uint96 additionalDeposit)",
        f"function requestClose({_CR_DESC} descriptor)",
        f"function settle({_CR_DESC} descriptor, uint96 cumulativeAmount, bytes signature)",
        f"function getChannelState(bytes32 channelId) view returns ({_CR_STATE})",
        "function getVoucherDigest(bytes32 channelId, uint96 cumulativeAmount) view returns (bytes32)",
    ]
)
