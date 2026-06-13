"""Tiny environment-variable helpers shared across the codebase."""

from __future__ import annotations


def truthy(value: str | None) -> bool:
    """Whether an environment-variable string represents an enabled flag.

    The single canonical parser for boolean env flags — ``"1"``, ``"true"``,
    ``"yes"``, ``"on"`` (case- and whitespace-insensitive) are enabled; anything
    else (including ``None`` and the empty string) is disabled.
    """
    return (value or "").strip().lower() in ("1", "true", "yes", "on")
