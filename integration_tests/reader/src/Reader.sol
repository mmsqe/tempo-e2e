// SPDX-License-Identifier: LGPL-3.0-only
pragma solidity ^0.8.28;

import {Anchoring} from "anchoring/Anchoring.sol";

/// Anchoring plus one bulk read. Injected over the contract's code by an eth_call state
/// override, so it reads the contract's own storage; it is never deployed.
contract Reader is Anchoring {
    constructor() Anchoring(address(0)) {}

    /// Every version of records `fromRecordId` .. `fromRecordId + count - 1` of `registryId`,
    /// by record id then index, stopping at the registry's last record.
    function versions(uint64 registryId, uint64 fromRecordId, uint64 count)
        external
        view
        returns (Record[] memory out)
    {
        require(fromRecordId != 0 && count != 0, "range");
        uint64 last = _recordCount[registryId];
        if (last > fromRecordId + count - 1) last = fromRecordId + count - 1;

        uint256 n;
        for (uint64 r = fromRecordId; r <= last; r++) {
            n += _latestIndex[registryId][r];
        }
        out = new Record[](n);
        uint256 k;
        for (uint64 r = fromRecordId; r <= last; r++) {
            uint64 top = _latestIndex[registryId][r];
            for (uint64 i = 1; i <= top; i++) {
                out[k++] = _records[registryId][r][i];
            }
        }
    }
}
