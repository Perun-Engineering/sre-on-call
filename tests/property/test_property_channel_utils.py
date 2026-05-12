"""Property-based tests for channel selection utilities."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from shared.channel_utils import select_channels

# Strategy: generate random Slack-style channel ID strings.
# Real Slack channel IDs are alphanumeric strings starting with 'C' or 'G',
# but for property testing we use arbitrary short strings.
_channel_id = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=15,
)

_channel_id_lists = st.lists(_channel_id, min_size=0, max_size=50)


@settings(max_examples=150)
@given(channel_ids=_channel_id_lists)
def test_channel_scan_respects_maximum_limit(
    channel_ids: list[str],
) -> None:
    """
    For any list of channel IDs with length N:
    1. len(result) <= min(N, 10)
    2. All returned channels are a subset of the input list
    """
    result = select_channels(channel_ids)

    # 1. At most min(len(channel_ids), 10) channels returned
    assert len(result) <= min(len(channel_ids), 10), (
        f"Expected at most {min(len(channel_ids), 10)} channels, got {len(result)}"
    )

    # 2. Every returned channel must be present in the input list
    for ch in result:
        assert ch in channel_ids, f"Returned channel {ch!r} is not in the input list"
