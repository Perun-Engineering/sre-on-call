"""Platform-agnostic chat poster abstraction.

Provides a ChatPoster protocol and concrete implementations for each
supported chat platform (Slack, Discord).  The factory function
``create_chat_poster`` selects the right implementation based on the
platform string stored in AlertContext.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from shared.models import AlertContext
from shared.secrets import resolve_secret

logger = logging.getLogger(__name__)

# Retry configuration for chat API calls
CHAT_MAX_RETRIES: int = 3
CHAT_BASE_DELAY: float = 1.0  # seconds; delays are 1s, 2s, 4s


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ChatPoster(Protocol):
    """Minimal interface for posting messages to a chat platform."""

    async def post_reply(self, alert_context: AlertContext, text: str) -> None:
        """Post *text* as a reply in the thread/channel of the alert."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Slack implementation
# ---------------------------------------------------------------------------


class SlackChatPoster:
    """ChatPoster backed by ``slack_sdk``."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or resolve_secret("SLACK_BOT_TOKEN")

    async def post_reply(self, alert_context: AlertContext, text: str) -> None:
        from slack_sdk.web.async_client import AsyncWebClient

        thread_ts = alert_context.platform_metadata.get(
            "thread_ts", alert_context.message_id
        )
        client = AsyncWebClient(token=self._token)
        await client.chat_postMessage(
            channel=alert_context.channel_id,
            thread_ts=thread_ts,
            text=text,
        )


# ---------------------------------------------------------------------------
# Discord implementation
# ---------------------------------------------------------------------------


class DiscordChatPoster:
    """ChatPoster backed by ``discord.py``."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or resolve_secret("DISCORD_BOT_TOKEN")

    async def post_reply(self, alert_context: AlertContext, text: str) -> None:
        import aiohttp

        channel_id = alert_context.channel_id
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }
        payload: dict = {"content": text}

        # Reply to the original message if we have a reference
        message_ref = alert_context.platform_metadata.get("message_id")
        if message_ref:
            payload["message_reference"] = {"message_id": message_ref}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise RuntimeError(
                        f"Discord API error {resp.status}: {body}"
                    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_POSTER_REGISTRY: dict[str, type] = {
    "slack": SlackChatPoster,
    "discord": DiscordChatPoster,
}


def create_chat_poster(platform: str, **kwargs) -> ChatPoster:
    """Create a ChatPoster for the given platform.

    Raises:
        ValueError: If *platform* is not supported.
    """
    cls = _POSTER_REGISTRY.get(platform)
    if cls is None:
        raise ValueError(
            f"Unsupported platform: {platform!r}. "
            f"Supported: {', '.join(_POSTER_REGISTRY)}"
        )
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Retry helper (platform-agnostic)
# ---------------------------------------------------------------------------


async def chat_post_with_retry(
    poster: ChatPoster,
    alert_context: AlertContext,
    text: str,
    *,
    max_retries: int = CHAT_MAX_RETRIES,
    base_delay: float = CHAT_BASE_DELAY,
) -> None:
    """Post a chat reply with exponential-backoff retries."""
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            await poster.post_reply(alert_context, text)
            return
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "Chat post attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
