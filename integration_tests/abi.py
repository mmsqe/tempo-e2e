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

# AccountKeychain views used only by tests (not part of the tempo-py bindings).
KEYCHAIN_VIEWS = Contract.from_abi(
    [
        "function getRemainingLimitWithPeriod(address account, address keyId, address token)"
        " view returns (uint256 remaining, uint64 periodEnd)",
        "function getKey(address account, address keyId)"
        " view returns ((uint8 signatureType, address keyId, uint64 expiry, bool enforceLimits, bool isRevoked))",
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


# Anchoring precompile (IAnchoring): a caller-partitioned commitment log, enshrined at T10.
# The caller is the namespace, so there is no authorization surface and nothing to deploy.
# Supersedes the withdrawn x/anchoring module: the address is inherited from that
# precompile, but its selectors are gone and now revert UnknownFunctionSelector. Registry and
# record reads become indexer queries over the Anchored log; roles have no successor at all --
# there is no grantRole/hasRole here, and permissioning is a wrapper-contract concern.
ANCHORING_ADDRESS = to_checksum_address("0x0000000000000000000000000000000000000a00")
ANCHORING = Contract.from_abi(
    [
        "function anchor(bytes32 key, bytes32 commitment, bytes metadata)",
        "function anchorAndHash(bytes32 key, bytes metadata)",
        "function latest(address namespace, bytes32 key) view returns (bytes32 commitment)",
        "event Anchored(address indexed caller, bytes32 indexed key, bytes32 commitment, bytes metadata)",
    ]
)

# Registry (app contract): one deployment per registry -- checksum records with scoped RBAC,
# anchored through the precompile rather than stored on-chain. There is no registryId anywhere:
# the deployment address is the partition, because the precompile is a caller-partitioned log.
# The contract keeps only role membership and a version count per record; latest(registry, key)
# in the precompile is the source of truth, and every anchored envelope leads with a bytes32
# kind (record/status) an indexer classifies on. Roles are registry- or record-scoped (one
# checksum) over "admin"/"editor" as right-padded bytes32, and are not anchored: membership is
# this contract's state, its history the contract's own events.
REGISTRY = Contract.from_abi(
    [
        "function addRecord(string uri, string checksum, string checksumAlgo, string metadata,"
        " uint8 category, string dataPointer) returns (bytes32 checksumHash, uint256 index)",
        "function updateRecordStatus(string checksum, uint256 index, string status)",
        "function grantRole(string checksum, address account, bytes32 role)",
        "function revokeRole(string checksum, address account, bytes32 role)",
        "function hasRole(string checksum, address account, bytes32 role) view returns (bool)",
        "function versionCount(bytes32 checksumHash) view returns (uint256)",
        "function latestRecordDigest(bytes32 checksumHash) view returns (bytes32)",
        # The kind tags leading every anchored envelope; indexers match these literals.
        "function KIND_RECORD() pure returns (bytes32)",
        "function KIND_STATUS() pure returns (bytes32)",
        # keccak256(""), the checksumHash a registry-scoped role is announced under.
        "function REGISTRY_SCOPE() pure returns (bytes32)",
        "function recordKey(bytes32 checksumHash) pure returns (bytes32)",
        "function statusKey(bytes32 checksumHash, uint256 index) pure returns (bytes32)",
        "function ROLE_ADMIN() pure returns (bytes32)",
        "function ROLE_EDITOR() pure returns (bytes32)",
        "function owner() view returns (address)",
        "event RecordAdded(bytes32 indexed checksumHash, uint256 index, string checksum,"
        " uint8 category, string dataPointer, address indexed author)",
        "event RecordStatusUpdated(bytes32 indexed checksumHash, uint256 index, string status)",
        "event RoleGranted(bytes32 indexed checksumHash, address indexed account, bytes32 role)",
        "event RoleRevoked(bytes32 indexed checksumHash, address indexed account, bytes32 role)",
    ]
)

# The factory: deploys one Registry per registry, outright and immutable -- upgrading means a
# new registry and its roles granted again. Registry name/description/metadata ride in the deployment
# event rather than an anchor -- descriptive, set once, nothing to prove -- and there is no
# on-chain set of registries either: the log is the record, so enumeration is an indexer's job.
REGISTRY_FACTORY = Contract.from_abi(
    [
        "function deployRegistry(string name, string description, string metadata) returns (address registry)",
        "function owner() view returns (address)",
        # Ownable's. `renounceOwnership` is declared only so a test can watch it be refused.
        "function transferOwnership(address newOwner)",
        "function renounceOwnership()",
        "event RegistryDeployed(address indexed registry, address indexed creator, string name, string description, string metadata)",
    ]
)

# Its one-shot deployer: a single create tx deploys the factory outright -- no proxy and
# nothing to upgrade, since a replacement registry splits history across two addresses rather
# than invalidating any -- with the calling EOA as its owner, and so as every registry's
# break-glass admin. Read the factory from `factory()`.
REGISTRY_DEPLOYER = Contract.from_abi(["function factory() view returns (address)"])
# Test mock ERC-20 (open mint), stood up wherever a plain token is needed.
MOCK_ERC20 = Contract.from_abi(["function mint(address to, uint256 amount)"])

# NVNMStaking (upgradeable app contract): delegated staking that shares chain fees with stakers.
# Stake NVNM toward a validator, deposited reward-token (nvmnUSD) is split pro-rata per validator.
STAKING = Contract.from_abi(
    [
        "function stake(address validator, uint256 amount)",
        "function unstake(address validator, uint256 amount)",
        "function depositReward(address validator, uint256 amount)",
        "function compoundReward(address validator, uint256 amount)",
        "function claim(address validator) returns (uint256 amount)",
        "function earned(address validator, address user) view returns (uint256)",
        "function stakedOf(address validator, address user) view returns (uint256)",
        "function totalStaked(address validator) view returns (uint256)",
        "function totalShares(address validator) view returns (uint256)",
        "function stakeToken() view returns (address)",
        "function rewardToken() view returns (address)",
        # election: top-`maxCommittee` by acquired*acquiredWeight + delegated, one equal
        # seat each — the consensus engine is unit-weighted, so the committee is just the
        # address list. Seating fewer than `minSeats` members elects nobody (registry
        # fallback on every node at once).
        "function setCandidate(address validator, bool active)",
        "function setCommitteeConfig(uint256 maxCommittee, uint256 acquiredWeight, uint256 maxDelegated)",
        "function candidates() view returns (address[])",
        "function computeCommittee() view returns (address[] vals)",
        "function setMinSeats(uint256 minSeats)",
        "function minSeats() view returns (uint256)",
        # candidacy: self-register against an NVNM bond. `minAcquired` is the 1M NVNM floor —
        # below it, delegated stake alone never buys a seat.
        "function setCandidacyBond(uint256 bond)",
        "function registerCandidate()",
        "function resignCandidate()",
        "function bondOf(address validator) view returns (uint256)",
        "function setMinAcquired(uint256 minAcquired)",
        "function minAcquired() view returns (uint256)",
        # slashing: system caller (address(0)) or owner seizes the candidacy bond, including one
        # unbonding after a resignation. Delegated stake is never slashed.
        "function slash(address validator, uint256 bps, address recipient) returns (uint256 seized)",
        # unbonding: with a period set, exiting stake and a resigned bond each park in a pending
        # bucket until their own withdrawal.
        "function setUnbondingPeriod(uint256 period)",
        "function withdraw(address validator) returns (uint256 amount)",
        "function pendingUnstakeOf(address validator, address user) view returns (uint256 amount, uint256 releaseAt)",
        "function withdrawBond() returns (uint256 amount)",
        "function pendingBondOf(address validator) view returns (uint256 amount, uint256 releaseAt)",
    ]
)

# Its one-shot deployer (nvnmchain-contracts StakingDeployer.sol): stands up mock NVNM + reward
# tokens and the staking proxy, and exposes their addresses.
STAKING_DEPLOYER = Contract.from_abi(
    [
        "function staking() view returns (address)",
        "function nvnm() view returns (address)",
        "function usd() view returns (address)",
    ]
)

# FeeRouter: protocol cuts (devshare/buybacks) then validator remainder → commission + delegators.
FEE_ROUTER = Contract.from_abi(
    [
        # FeeManager swaps each payer's fee into the recipient's preferred token, so a router
        # normally holds one and the bare overload covers it. `flush(token)` routes anything
        # else — a preferred token pointed away from the pool's reward token, or a transfer in.
        "function flush() returns (uint256 deposited)",
        "function flush(address token) returns (uint256 deposited)",
        "function validator() view returns (address)",
        "function commissionBps() view returns (uint256)",
        # Read live off the staking proxy, so it follows a reward-token migration.
        "function rewardToken() view returns (address)",
        # Delegator share held for a token the pool cannot account in, kept out of the
        # flushable balance so a permissionless re-flush cannot cut it twice.
        "function heldForDelegators(address token) view returns (uint256)",
    ]
)
FEE_ROUTER_FACTORY = Contract.from_abi(
    [
        "function create(address validator, address operator, uint256 commissionBps) returns (address router)",
        "function setSwapper(address swapper)",
        "function setProtocolSplit(address devshare, address buyback, uint256 devshareBps, uint256 buybackBps)",
        # GuardedSwapper reads this to decide who may move its reference price.
        "function isRouter(address account) view returns (bool)",
        "event RouterCreated(address indexed validator, address router, address operator, uint256 commissionBps)",
    ]
)
