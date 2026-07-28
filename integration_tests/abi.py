"""ABIs for tempo precompiles. Standard token ops reuse ``eth_contract.erc20.ERC20``."""

from eth_contract import Contract
from eth_utils import to_checksum_address

# 2D nonce precompile (INonce). Nonce key 0 is the protocol nonce and reverts here.
NONCE = Contract.from_abi(["function getNonce(address account, uint256 nonceKey) view returns (uint64)"])

# Stablecoin DEX precompile (IStablecoinDEX): order book keyed against PATH_USD.
DEX = Contract.from_abi(
    [
        "function place(address token, uint128 amount, bool isBid, int16 tick) returns (uint128 orderId)",
        "function placeFlip(address token, uint128 amount, bool isBid, int16 tick, int16 flipTick) returns (uint128 orderId)",
        "function cancel(uint128 orderId)",
        "function createPair(address base) returns (bytes32 key)",
        "function balanceOf(address user, address token) view returns (uint128)",  # internal (escrow) balance
        "function withdraw(address token, uint128 amount)",
        "function swapExactAmountIn(address tokenIn, address tokenOut, uint128 amountIn, uint128 minAmountOut) returns (uint128 amountOut)",
        "function quoteSwapExactAmountIn(address tokenIn, address tokenOut, uint128 amountIn) view returns (uint128 amountOut)",
        "function getOrder(uint128 orderId) view returns ((uint128 orderId, address maker, bytes32 key, bool isBid, int16 tick, uint128 amount, uint128 remaining, uint128 prev, uint128 next, bool isFlip, int16 flipTick))",
        "function nextOrderId() view returns (uint128)",
        "function pairKey(address a, address b) pure returns (bytes32)",
        "function tickToPrice(int16 tick) pure returns (uint32)",
        "function priceToTick(uint32 price) pure returns (int16)",
        "function MIN_ORDER_AMOUNT() pure returns (uint128)",
        "function storageCredits(address user) view returns (uint64)",  # TIP-1064 reusable-order credits
        # TIP-1087 (T8+): a book carries a compact index into the append-only book_keys vector,
        # so orders can store the 4-byte index instead of the 32-byte key.
        "function bookIndexForKey(bytes32 bookKey) view returns (bool set, uint32 index)",
        "function bookKeyForIndex(uint32 index) view returns (bytes32 bookKey)",
        "function setBookIndex(uint32 index)",
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
        "function distributeFees(address validator, address token)",  # permissionless payout to the fee recipient
        "function collectedFees(address validator, address token) view returns (uint256)",
    ]
)

# TIP-20 token factory (ITIP20Factory) and AccessControl on the created tokens.
TIP20_FACTORY = Contract.from_abi(
    [
        "function createToken(string name, string symbol, string currency, address quoteToken, address admin, bytes32 salt) returns (address)",
        "function createToken(string name, string symbol, string currency, address quoteToken, address admin, bytes32 salt, string logoURI) returns (address)",
        "function isTIP20(address token) view returns (bool)",
    ]
)
TIP20_ROLES = Contract.from_abi(["function grantRole(bytes32 role, address account)"])

# AccountKeychain view used only by tests (not part of the tempo-py bindings).
KEYCHAIN_VIEWS = Contract.from_abi(
    [
        "function getRemainingLimitWithPeriod(address account, address keyId, address token)"
        " view returns (uint256 remaining, uint64 periodEnd)",
    ]
)

# Tempo TIP-20 extensions beyond ERC-20 (standard ops use eth_contract.erc20.ERC20).
TIP20 = Contract.from_abi(
    [
        "function transferWithMemo(address to, uint256 amount, bytes32 memo)",
        "function burn(uint256 amount)",
        "function changeTransferPolicyId(uint64 newPolicyId)",
        "function transferPolicyId() view returns (uint64)",
        "function logoURI() view returns (string)",
        "function setLogoURI(string newLogoURI)",
    ]
)

# EIP-2612 permit on TIP-20 tokens (TIP-1004, T2+). The 712 domain is
# {name: token.name(), version: "1", chainId, verifyingContract: token}.
TIP20_PERMIT = Contract.from_abi(
    [
        "function permit(address owner, address spender, uint256 value, uint256 deadline, uint8 v, bytes32 r, bytes32 s)",
        "function nonces(address owner) view returns (uint256)",
        "function DOMAIN_SEPARATOR() view returns (bytes32)",
        "function name() view returns (string)",
    ]
)

# TIP-20 rewards, deprecated by TIP-1075: at T7 setRewardRecipient/distributeReward
# are silent no-ops and transfers no longer accrue.
TIP20_REWARDS = Contract.from_abi(
    [
        "function setRewardRecipient(address recipient)",
        "function distributeReward(uint256 amount)",
        "function claimRewards() returns (uint256)",
        "function optedInSupply() view returns (uint128)",
        "function globalRewardPerToken() view returns (uint256)",
        "function getPendingRewards(address account) view returns (uint128)",
        "function userRewardInfo(address account) view returns (address rewardRecipient, uint256 rewardPerToken, uint256 rewardBalance)",
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
        # Compound policies (TIP-1015, T2+): three simple sub-policies dispatched by role.
        "function createCompoundPolicy(uint64 senderPolicyId, uint64 recipientPolicyId, uint64 mintRecipientPolicyId) returns (uint64)",
        "function isAuthorizedSender(uint64 policyId, address user) view returns (bool)",
        "function isAuthorizedRecipient(uint64 policyId, address user) view returns (bool)",
        "function isAuthorizedMintRecipient(uint64 policyId, address user) view returns (bool)",
        "function compoundPolicyData(uint64 policyId) view returns (uint64 senderPolicyId, uint64 recipientPolicyId, uint64 mintRecipientPolicyId)",
        # Receive policies (TIP-1028, T6+): a receiver sets which senders/tokens it accepts.
        "function setReceivePolicy(uint64 senderPolicyId, uint64 tokenFilterId, address recoveryAuthority)",
        "function validateReceivePolicy(address token, address sender, address receiver) view returns (bool authorized, uint8 blockedReason)",
    ]
)

# Receive policy guard precompile (IReceivePolicyGuard, TIP-1028, T6+): a transfer blocked
# by the recipient's receive policy is escrowed here (not reverted); the receiver claims it.
# `receipt` is the self-describing witness bytes emitted in the TransferBlocked event.
RECEIVE_POLICY_GUARD = Contract.from_abi(
    [
        "function balanceOf(bytes receipt) view returns (uint256 amount)",
        "function claim(address to, bytes receipt)",
        "function burnBlockedReceipt(bytes receipt)",
    ]
)

# Validator config precompiles (IValidatorConfig / IValidatorConfigV2); validatorCount is common.
VALIDATOR_CONFIG = Contract.from_abi(["function validatorCount() view returns (uint64)"])

# ValidatorConfig V2 (TIP-1017, 0xCccC…01): append-only validator registry.
_VALIDATOR_TUPLE = (
    "(bytes32 publicKey, address validatorAddress, string ingress, string egress,"
    " address feeRecipient, uint64 index, uint64 addedAtHeight, uint64 deactivatedAtHeight)"
)
VALIDATOR_CONFIG_V2 = Contract.from_abi(
    [
        "function owner() view returns (address)",
        "function isInitialized() view returns (bool)",
        "function getInitializedAtHeight() view returns (uint64)",
        "function validatorCount() view returns (uint64)",
        f"function getActiveValidators() view returns ({_VALIDATOR_TUPLE}[])",
        "function getNextNetworkIdentityRotationEpoch() view returns (uint64)",
        "function addValidator(address validatorAddress, bytes32 publicKey, string ingress, string egress,"
        " address feeRecipient, bytes signature) returns (uint64)",
        "function transferOwnership(address newOwner)",
    ]
)

# Current committee precompile (ICurrentCommittee, TIP-1070, T8+): the committee picked by
# the epoch-boundary DKG outcome, written by a system call.
CURRENT_COMMITTEE_ADDRESS = to_checksum_address("0xC077E00000000000000000000000000000000000")
CURRENT_COMMITTEE = Contract.from_abi(
    [
        "function getCommitteeMembers() view returns (uint64 epoch, bytes32[] publicKeys)",
        "function setCommitteeMembers(uint64 epoch, bytes32[] publicKeys)",  # system-only: msg.sender must be 0x0
    ]
)

# Address registry precompile (IAddressRegistry, TIP-1022, T3+): virtual-address forwarding.
# A master registers with a proof-of-work salt; deposits to a derived virtual address
# (masterId ‖ 0xFD*10 ‖ userTag) are forwarded to the master by the TIP-20 transfer path.
ADDRESS_REGISTRY = Contract.from_abi(
    [
        "function registerVirtualMaster(bytes32 salt) returns (bytes4 masterId)",
        "function getMaster(bytes4 masterId) view returns (address)",
        "function resolveRecipient(address to) view returns (address)",
        "function resolveVirtualAddress(address virtualAddr) view returns (address)",
        "function isVirtualAddress(address addr) pure returns (bool)",
        "function decodeVirtualAddress(address addr) pure returns (bool isVirtual, bytes4 masterId, bytes6 userTag)",
        "function isImplicitlyApproved(address addr) view returns (bool)",  # TIP-1035 implicit-approval list
    ]
)

# Signature verifier precompile (ISignatureVerifier, TIP-1020, T3+; verifyKeychain* are T6+).
# recover/verify take a tempo signature (a plain 65-byte secp256k1 blob has no type prefix).
SIGNATURE_VERIFIER = Contract.from_abi(
    [
        "function recover(bytes32 hash, bytes signature) view returns (address)",
        "function verify(address signer, bytes32 hash, bytes signature) view returns (bool)",
        "function verifyKeychain(address account, bytes32 hash, bytes signature) view returns (bool)",
        "function verifyKeychainAdmin(address account, bytes32 hash, bytes signature) view returns (bool)",
    ]
)

# Storage credits precompile (IStorageCredits, TIP-1060, T7+): deleting a storage slot
# mints a credit to the slot's owner; mode/budget (Refund=0/Preserve=1/Direct=2) are
# transaction-local. mode 3 is reserved -> InvalidMode().
STORAGE_CREDITS = Contract.from_abi(
    [
        "function balanceOf(address account) view returns (uint64)",
        "function modeOf(address account) view returns (uint8)",
        "function budgetOf(address account) view returns (uint64)",
        "function setMode(uint8 newMode)",
        "function setBudget(uint64 credits)",
    ]
)

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
        # close is payee/operator-side and bypasses the grace period; withdraw is payer-side and timed.
        f"function close({_CR_DESC} descriptor, uint96 cumulativeAmount, uint96 captureAmount, bytes signature)",
        f"function withdraw({_CR_DESC} descriptor)",
        f"function getChannelState(bytes32 channelId) view returns ({_CR_STATE})",
        "function getVoucherDigest(bytes32 channelId, uint96 cumulativeAmount) view returns (bytes32)",
    ]
)


# AnchoringRegistry (upgradeable app contract, NOT a precompile): named registries hold checksum
# records, versioned per checksum, with RBAC. Roles: ADMIN=1, REGISTRAR=2, STATUS_UPDATER=3.
_RECORD = (
    "(uint256 registryId, string uri, string checksum, string checksumAlgo, string metadata, "
    "uint256 timestamp, string status, uint256 recordId, uint256 index, bool isLatest)"
)
ANCHORING = Contract.from_abi(
    [
        "function addRegistry(string name, string description, string metadata) returns (uint256 id)",
        "function addRecord(string registry, string uri, string checksum, string checksumAlgo, string metadata) returns (uint256 recordId, uint256 index)",
        "function updateRecordStatus(uint256 registryId, uint256 recordId, uint256 index, string status)",
        "function grantRole(address holder, uint256 role)",
        "function hasRole(address holder, uint256 role) view returns (bool)",
        "function registryCount() view returns (uint256)",
        "function registryIdByName(string name) view returns (uint256)",
        "function recordIdForChecksum(uint256 registryId, string checksum) view returns (uint256)",
        "function versionCount(uint256 registryId, uint256 recordId) view returns (uint256)",
        "function checksumRefCount(string checksum) view returns (uint256)",
        f"function getRecord(uint256 registryId, uint256 recordId, uint256 index) view returns ({_RECORD})",
        "function queryByChecksum(string checksum, uint256 limit) view returns ((uint256 registryId, uint256 recordId)[])",
    ]
)

# The one-shot deployer helper (tempo/contracts AnchoringDeployer.sol): its create tx stands up
# impl + proxy + init and exposes the proxy address.
ANCHORING_DEPLOYER = Contract.from_abi(["function registry() view returns (address)"])


# NVNMStaking (upgradeable app contract): delegated staking that shares chain fees with stakers.
# Stake NVNM toward a validator, deposited reward-token (nvmnUSD) is split pro-rata per validator.
STAKING = Contract.from_abi(
    [
        "function stake(address validator, uint256 amount)",
        "function unstake(address validator, uint256 amount)",
        "function depositReward(address validator, uint256 amount)",
        "function claim(address validator) returns (uint256 amount)",
        "function earned(address validator, address user) view returns (uint256)",
        "function stakedOf(address validator, address user) view returns (uint256)",
        "function totalStaked(address validator) view returns (uint256)",
        "function stakeToken() view returns (address)",
        "function rewardToken() view returns (address)",
        # committee election (quantized multi-seat): seats = min(stake/tokensPerSeat, cap),
        # ranked by stake, truncated to the maxSeats budget.
        "function setCandidate(address validator, bool active)",
        "function setSeatConfig(uint256 tokensPerSeat, uint256 maxSeats, uint256 maxSeatsPerValidator)",
        "function candidates() view returns (address[])",
        "function computeCommittee() view returns (address[] vals, uint256[] seats)",
        # bonded candidacy: self-register against an NVNM bond (refunded on resign/kick).
        "function setCandidacyBond(uint256 bond)",
        "function registerCandidate()",
        "function resignCandidate()",
        "function bondOf(address validator) view returns (uint256)",
        # slashing: system caller (address(0)) or owner cuts a validator pool pro-rata in O(1);
        # pending (unbonding) stake is cut too via the per-validator factor.
        "function slash(address validator, uint256 bps, address recipient) returns (uint256 seized)",
        # unbonding: with a period set, unstake parks stake in a pending bucket until withdraw.
        "function setUnbondingPeriod(uint256 period)",
        "function withdraw(address validator) returns (uint256 amount)",
        "function pendingUnstakeOf(address validator, address user) view returns (uint256 amount, uint256 releaseAt)",
    ]
)

# FeeRouter (app contract): per-validator fee splitter — commission to the operator, remainder
# deposited into the staking pool. `flush` is permissionless.
FEE_ROUTER = Contract.from_abi(
    [
        "function flush() returns (uint256 deposited)",
        "function validator() view returns (address)",
        "function commissionBps() view returns (uint256)",
    ]
)

# The one-shot deployer helper (tempo/contracts StakingDeployer.sol): stands up mock NVNM + reward
# tokens and the staking proxy, and exposes their addresses.
STAKING_DEPLOYER = Contract.from_abi(
    [
        "function staking() view returns (address)",
        "function nvnm() view returns (address)",
        "function usd() view returns (address)",
    ]
)
