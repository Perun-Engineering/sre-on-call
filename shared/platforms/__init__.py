"""ChatPlatform — the seam between the investigation pipeline and a chat platform.

A ChatPlatform owns the full per-platform lifecycle:

  * **ingest** a webhook  — verify signature, classify the request into one of
    a tagged set of :class:`WebhookEvent` variants (challenge, alert,
    slash command, or invalid).
  * **ack** a slash command — synchronous platform callback (Slack
    ``response_url`` POST, Discord interaction callback).
  * **deliver** investigation output — render the structured payload
    (:class:`ReportSections`, :class:`EnrichmentSections`,
    :class:`InvestigationStartedSections`, :class:`PIRSections`) to
    platform-native markup and post it as a thread reply on the
    originating message.

Slack and Discord each provide one implementation under
``shared/platforms/<name>.py``. This subsumes the legacy ``WebhookAdapter``,
``ChatPoster``, and ``ReportRenderer`` seams; during the migration the
implementations delegate to the legacy classes via lazy imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Union

from shared.models import AlertContext, CommandRequest
from shared.report_renderer import (
    EnrichmentSections,
    InvestigationStartedSections,
    PIRSections,
    ReportSections,
    SnapshotSections,
)


# ---------------------------------------------------------------------------
# WebhookEvent — tagged union returned by ChatPlatform.ingest()
# ---------------------------------------------------------------------------


@dataclass
class InvalidWebhook:
    """Webhook failed verification or could not be parsed.

    ``status_code`` is the HTTP status the caller should return (401 for
    failed signature, 400 for malformed body); ``reason`` is a short human
    string that goes into the response body.
    """

    status_code: int
    reason: str


@dataclass
class ChallengeWebhook:
    """Platform challenge — Slack ``url_verification`` or Discord PING.

    ``response`` is the JSON body the platform expects in the HTTP 200.
    """

    response: dict


@dataclass
class AlertWebhook:
    """A genuine alert event ready to invoke the master agent."""

    context: AlertContext


@dataclass
class CommandWebhook:
    """A slash-command invocation requiring an ack + master invocation."""

    command: CommandRequest


WebhookEvent = Union[InvalidWebhook, ChallengeWebhook, AlertWebhook, CommandWebhook]


# ---------------------------------------------------------------------------
# DeliveryTarget — the routing-only value ChatPlatform.deliver posts to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryTarget:
    """Where a :meth:`ChatPlatform.deliver` call posts.

    ``thread_anchor`` is the originating message id to reply under; ``None``
    posts at top-level. Each platform interprets it natively (Slack
    ``thread_ts``, Discord ``message_reference.message_id``). Replaces
    threading routing through a (sometimes synthetic) :class:`AlertContext`.
    """

    platform: str  # "slack" | "discord"
    channel_id: str
    thread_anchor: str | None = None

    @classmethod
    def for_alert(cls, alert_context: AlertContext) -> "DeliveryTarget":
        """Project an alert's routing fields — reply threaded under the alert."""
        return cls(
            platform=alert_context.platform,
            channel_id=alert_context.channel_id,
            thread_anchor=alert_context.message_id or None,
        )


# ---------------------------------------------------------------------------
# Section payload union for ChatPlatform.deliver()
# ---------------------------------------------------------------------------


DeliverPayload = Union[
    ReportSections,
    EnrichmentSections,
    InvestigationStartedSections,
    PIRSections,
    SnapshotSections,
]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ChatPlatform(Protocol):
    """Seam between the investigation pipeline and a chat platform."""

    name: str  # e.g. "slack", "discord"

    def ingest(self, headers: dict, raw_body: str) -> WebhookEvent:
        """Classify a webhook request into a tagged :class:`WebhookEvent`."""
        ...  # pragma: no cover

    def ack(self, command: CommandRequest, text: str) -> None:
        """Send the synchronous slash-command callback expected by the platform."""
        ...  # pragma: no cover

    async def deliver(
        self, target: "DeliveryTarget", payload: DeliverPayload
    ) -> str:
        """Render *payload* in platform-native markup, post it to *target*,
        and return the rendered text.

        The return value lets callers (e.g. the experiment results store) keep
        a single source of truth for "what the user saw" without re-rendering.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------


async def deliver_with_retry(
    platform: ChatPlatform,
    target: "DeliveryTarget",
    payload: DeliverPayload,
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> str:
    """Call ``platform.deliver`` with exponential-backoff retries.

    Returns the rendered text on success; raises the last exception when all
    attempts fail. Delays follow ``base_delay * 2**attempt``.
    """
    import asyncio
    import logging

    logger = logging.getLogger(__name__)

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await platform.deliver(target, payload)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "deliver attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def for_platform(name: str) -> ChatPlatform:
    """Return a :class:`ChatPlatform` instance by platform name.

    Raises :class:`ValueError` when *name* is not a supported platform.
    """
    if name == "slack":
        from shared.platforms.slack import SlackChatPlatform

        return SlackChatPlatform()
    if name == "discord":
        from shared.platforms.discord import DiscordChatPlatform

        return DiscordChatPlatform()
    raise ValueError(f"Unsupported platform: {name!r}")


def detect_platform(headers: dict) -> ChatPlatform:
    """Sniff the platform from request headers and return its :class:`ChatPlatform`.

    Slack signs requests with ``x-slack-signature``/``x-slack-request-timestamp``;
    Discord uses ``x-signature-ed25519``/``x-signature-timestamp``. Raises
    :class:`ValueError` when neither set is present.
    """
    if "x-slack-signature" in headers or "x-slack-request-timestamp" in headers:
        return for_platform("slack")
    if "x-signature-ed25519" in headers or "x-signature-timestamp" in headers:
        return for_platform("discord")
    raise ValueError("Unable to detect platform from request headers")
