"""Channel selection utilities for sre-on-call."""

from shared.constants import MAX_CHANNELS


def select_channels(
    channel_ids: list[str], max_channels: int = MAX_CHANNELS
) -> list[str]:
    """Select a subset of channels to scan, respecting the maximum limit.

    Args:
        channel_ids: List of Slack channel IDs to select from.
        max_channels: Maximum number of channels to return. Defaults to
            ``MAX_CHANNELS`` (10).

    Returns:
        A list containing at most ``min(len(channel_ids), max_channels)``
        channels, all of which are elements of *channel_ids*.
    """
    return channel_ids[:max_channels]
