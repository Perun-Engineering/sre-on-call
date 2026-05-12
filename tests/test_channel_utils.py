"""Unit tests for shared.channel_utils."""

import pytest

from shared.channel_utils import select_channels
from shared.constants import MAX_CHANNELS


class TestSelectChannels:
    """Tests for the select_channels function."""

    def test_returns_all_when_under_limit(self):
        channels = ["C001", "C002", "C003"]
        result = select_channels(channels)
        assert result == channels

    def test_returns_max_channels_when_over_limit(self):
        channels = [f"C{i:03d}" for i in range(20)]
        result = select_channels(channels)
        assert len(result) == MAX_CHANNELS

    def test_returns_empty_for_empty_input(self):
        assert select_channels([]) == []

    def test_returns_exactly_max_when_equal_to_limit(self):
        channels = [f"C{i:03d}" for i in range(MAX_CHANNELS)]
        result = select_channels(channels)
        assert len(result) == MAX_CHANNELS
        assert result == channels

    def test_returned_channels_are_subset_of_input(self):
        channels = [f"C{i:03d}" for i in range(15)]
        result = select_channels(channels)
        assert all(ch in channels for ch in result)

    def test_custom_max_channels(self):
        channels = [f"C{i:03d}" for i in range(10)]
        result = select_channels(channels, max_channels=3)
        assert len(result) == 3

    def test_single_channel(self):
        result = select_channels(["C001"])
        assert result == ["C001"]
