"""Tests for the agent telemetry helpers."""

from __future__ import annotations

from shared.agent_telemetry import AGENT_METADATA, compute_cost_usd
from shared.models import AgentMetadata


class TestComputeCostUsd:
    def test_known_haiku_model(self):
        # Haiku 4.5 priced at $1/M input, $5/M output
        cost = compute_cost_usd(
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        assert cost == 6.0

    def test_unknown_model_returns_none(self):
        assert compute_cost_usd("not-a-real-model", 100, 100) is None

    def test_missing_tokens_returns_none(self):
        model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert compute_cost_usd(model, None, 100) is None
        assert compute_cost_usd(model, 100, None) is None

    def test_missing_model_returns_none(self):
        assert compute_cost_usd(None, 100, 100) is None


class TestMetadataFooterRoundTrip:
    def test_round_trip(self):
        original = AgentMetadata(
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            started_at="2025-01-01T00:00:00+00:00",
            completed_at="2025-01-01T00:00:05+00:00",
            input_tokens=10,
            output_tokens=20,
            total_tokens=30,
            cost_usd=0.00012,
        )
        text = "Agent says pods are healthy.\n\n" + AGENT_METADATA.encode(original)

        clean, recovered = AGENT_METADATA.extract(text)

        assert clean == "Agent says pods are healthy."
        assert recovered == original

    def test_no_footer_returns_text_unchanged(self):
        clean, meta = AGENT_METADATA.extract("plain agent reply")
        assert clean == "plain agent reply"
        assert meta is None

    def test_malformed_footer_dropped_silently(self):
        text = "reply\n\n<<<AGENT_METADATA not-json AGENT_METADATA>>>"
        clean, meta = AGENT_METADATA.extract(text)
        assert clean == "reply"
        assert meta is None

    def test_unknown_keys_in_footer_are_ignored(self):
        text = (
            "ok\n\n<<<AGENT_METADATA "
            '{"model_id": "x", "future_field": "ignore me"}'
            " AGENT_METADATA>>>"
        )
        _clean, meta = AGENT_METADATA.extract(text)
        assert meta is not None
        assert meta.model_id == "x"
