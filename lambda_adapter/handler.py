"""Single Lambda entry point for all webhook platforms.

Detects the platform from request headers, constructs the appropriate
WebhookAdapter, and delegates to the shared intake pipeline.
"""

from __future__ import annotations

from lambda_adapter.adapters import create_webhook_adapter, detect_platform
from lambda_adapter.intake import process_webhook


def lambda_handler(event: dict, context: object) -> dict:
    """Unified Lambda function URL entry point."""
    headers = event.get("headers", {})
    platform = detect_platform(headers)
    adapter = create_webhook_adapter(platform)
    return process_webhook(event, adapter)
