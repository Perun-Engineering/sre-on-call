"""Single Lambda entry point for all webhook platforms.

Detects the platform from request headers, constructs the appropriate
:class:`shared.platforms.ChatPlatform`, and delegates to the shared intake
pipeline.
"""

from __future__ import annotations

from lambda_adapter.intake import process_webhook
from shared.platforms import detect_platform


def lambda_handler(event: dict, context: object) -> dict:
    """Unified Lambda function URL entry point."""
    headers = event.get("headers", {})
    platform = detect_platform(headers)
    return process_webhook(event, platform)
