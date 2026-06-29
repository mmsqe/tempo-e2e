"""eth_feeHistory."""


async def test_fee_history_shape(w3):
    block_count, percentiles = 4, [25, 50, 75]
    fh = await w3.eth.fee_history(block_count, "latest", percentiles)
    assert len(fh["baseFeePerGas"]) == block_count + 1  # +1 for the next block
    assert len(fh["gasUsedRatio"]) == block_count
    assert all(0.0 <= ratio <= 1.0 for ratio in fh["gasUsedRatio"])
    assert fh["oldestBlock"] >= 0


async def test_fee_history_rewards_match_percentiles(w3):
    block_count, percentiles = 3, [10, 50, 90]
    fh = await w3.eth.fee_history(block_count, "latest", percentiles)
    assert len(fh["reward"]) == block_count
    assert all(len(row) == len(percentiles) for row in fh["reward"])
