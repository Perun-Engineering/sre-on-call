"""Unit tests for shared.platforms.slack — delivery behaviour."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from shared.platforms import DeliveryTarget
from shared.platforms.slack import SlackChatPlatform


@pytest.mark.asyncio
async def test_post_reply_disables_unfurl():
    platform = SlackChatPlatform(signing_secret="sec", bot_token="xoxb-test")

    fake_client = AsyncMock()
    with patch("slack_sdk.web.async_client.AsyncWebClient", return_value=fake_client):
        await platform._post_reply(
            DeliveryTarget(platform="slack", channel_id="C1", thread_anchor="123.45"),
            "hello",
        )
    _, kwargs = fake_client.chat_postMessage.call_args
    assert kwargs["unfurl_links"] is False
    assert kwargs["unfurl_media"] is False
