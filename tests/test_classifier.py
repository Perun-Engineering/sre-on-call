"""Unit tests for the intake alert classifier (Tier 1 heuristics + LLM seam)."""

from __future__ import annotations

import pytest

from lambda_adapter.classifier import (
    BedrockLlmClassifier,
    _parse_llm_verdict,
    classify_alert,
    classify_heuristic,
    llm_classifier_from_env,
)


# ---------------------------------------------------------------------------
# Tier 1 — confident ALERT
# ---------------------------------------------------------------------------


class TestHeuristicAlerts:
    @pytest.mark.parametrize(
        "text",
        [
            "CPU critical on api-server",
            "Service is DOWN",
            "PrometheusRule firing: HighErrorRate",
            "[FIRING:1] HighLatency (severity=critical)",
            "alertname=KubePodCrashLooping namespace=prod",
            "🚨 Database unreachable",
            "p1 outage in us-east-1",
            "5xx errors spiking on checkout",
            "pod stuck in CrashLoopBackOff",
        ],
    )
    def test_alert_shaped_messages_classified_alert(self, text):
        result = classify_heuristic(text)
        assert result is not None
        assert result.is_alert is True
        assert result.tier == "heuristic"

    def test_dashboard_link_is_alert(self):
        # Host carries no marker keyword, so the link pattern is what fires.
        text = "see https://metrics.internal.example.com/dashboards/123 for context"
        result = classify_heuristic(text)
        assert result is not None and result.is_alert is True
        assert result.reason == "dashboard/console link"

    def test_cloudwatch_console_link_is_alert(self):
        text = "logs at https://console.aws.amazon.com/cloudwatch/home"
        result = classify_heuristic(text)
        assert result is not None and result.is_alert is True


class TestManualOverride:
    def test_investigate_keyword_forces_alert(self):
        result = classify_heuristic("hey can you investigate this thread?")
        assert result is not None
        assert result.is_alert is True
        assert result.tier == "override"

    def test_override_wins_even_over_chatter(self):
        # "thanks" alone is chatter, but the override keyword forces an alert.
        result = classify_heuristic("thanks, please investigate")
        assert result is not None and result.is_alert is True
        assert result.tier == "override"


# ---------------------------------------------------------------------------
# Tier 1 — confident CHATTER
# ---------------------------------------------------------------------------


class TestHeuristicChatter:
    @pytest.mark.parametrize(
        "text",
        [
            "thanks!",
            "thank you",
            "thx",
            "hello",
            "hey there",
            "good morning",
            "lol nice",
            "got it",
            "who are you",
            "what can you do",
            "ping",
        ],
    )
    def test_chatter_classified_not_alert(self, text):
        result = classify_heuristic(text)
        assert result is not None
        assert result.is_alert is False
        assert result.tier == "heuristic"

    def test_empty_message_is_not_alert(self):
        result = classify_heuristic("   ")
        assert result is not None and result.is_alert is False
        assert result.reason == "empty message"

    def test_bare_mention_is_not_alert(self):
        # Slack renders "@bot" as a mention token that gets stripped.
        result = classify_heuristic("<@U12345>")
        assert result is not None and result.is_alert is False

    def test_thanks_for_the_alert_is_not_chatter(self):
        # Carries the word "alert" -> alert, not chatter (anchored chatter regex).
        result = classify_heuristic("thanks for the alert")
        assert result is not None and result.is_alert is True


# ---------------------------------------------------------------------------
# Tier 1 — ambiguous (defer)
# ---------------------------------------------------------------------------


class TestHeuristicAmbiguous:
    @pytest.mark.parametrize(
        "text",
        [
            "the deploy went out an hour ago and users in europe are seeing slow page loads now",
            "can someone take a look at the checkout flow when they get a chance please",
        ],
    )
    def test_ambiguous_returns_none(self, text):
        assert classify_heuristic(text) is None


# ---------------------------------------------------------------------------
# Combined classify_alert
# ---------------------------------------------------------------------------


class _FakeLlm:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def classify(self, alert_text):
        self.calls += 1
        return self.verdict


class TestClassifyAlert:
    def test_heuristic_alert_short_circuits_llm(self):
        llm = _FakeLlm(False)
        result = classify_alert("service is down", llm=llm)
        assert result.is_alert is True
        assert llm.calls == 0  # Tier 1 won; LLM never consulted

    def test_ambiguous_defaults_to_alert_without_llm(self):
        result = classify_alert(
            "users in europe are seeing slow page loads since the deploy"
        )
        assert result.is_alert is True
        assert result.tier == "default"

    def test_ambiguous_uses_llm_when_present(self):
        llm = _FakeLlm(False)
        result = classify_alert(
            "users in europe are seeing slow page loads since the deploy", llm=llm
        )
        assert llm.calls == 1
        assert result.is_alert is False
        assert result.tier == "llm"

    def test_llm_none_falls_back_to_alert(self):
        llm = _FakeLlm(None)
        result = classify_alert(
            "users in europe are seeing slow page loads since the deploy", llm=llm
        )
        assert result.is_alert is True
        assert result.tier == "default"


# ---------------------------------------------------------------------------
# Tier 2 — BedrockLlmClassifier
# ---------------------------------------------------------------------------


def _converse_response(text: str) -> dict:
    return {"output": {"message": {"content": [{"text": text}]}}}


class _FakeBedrock:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.kwargs = None

    def converse(self, **kwargs):
        self.kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return self._response


class TestBedrockLlmClassifier:
    def test_alert_verdict(self):
        client = _FakeBedrock(_converse_response("ALERT"))
        clf = BedrockLlmClassifier(model_id="m", client=client)
        assert clf.classify("db down") is True
        assert client.kwargs is not None and client.kwargs["modelId"] == "m"

    def test_chatter_verdict(self):
        client = _FakeBedrock(_converse_response("CHATTER"))
        clf = BedrockLlmClassifier(model_id="m", client=client)
        assert clf.classify("hi") is False

    def test_unparseable_verdict_returns_none(self):
        client = _FakeBedrock(_converse_response("maybe?"))
        clf = BedrockLlmClassifier(model_id="m", client=client)
        assert clf.classify("???") is None

    def test_client_error_is_fail_open_none(self):
        client = _FakeBedrock(exc=RuntimeError("throttled"))
        clf = BedrockLlmClassifier(model_id="m", client=client)
        assert clf.classify("???") is None

    def test_model_resolution_prefers_explicit(self, monkeypatch):
        monkeypatch.setenv("CLASSIFIER_MODEL_ID", "env-model")
        clf = BedrockLlmClassifier(client=_FakeBedrock(_converse_response("ALERT")))
        assert clf._model_id == "env-model"

    def test_guardrail_applied_when_configured(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
        monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "7")
        client = _FakeBedrock(_converse_response("ALERT"))
        BedrockLlmClassifier(model_id="m", client=client).classify("untrusted text")
        assert client.kwargs is not None
        assert client.kwargs["guardrailConfig"] == {
            "guardrailIdentifier": "gr-123",
            "guardrailVersion": "7",
        }

    def test_guardrail_version_defaults_to_draft(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
        monkeypatch.delenv("BEDROCK_GUARDRAIL_VERSION", raising=False)
        client = _FakeBedrock(_converse_response("ALERT"))
        BedrockLlmClassifier(model_id="m", client=client).classify("x")
        assert client.kwargs is not None
        assert client.kwargs["guardrailConfig"]["guardrailVersion"] == "DRAFT"

    def test_no_guardrail_key_when_unset(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GUARDRAIL_ID", raising=False)
        client = _FakeBedrock(_converse_response("ALERT"))
        BedrockLlmClassifier(model_id="m", client=client).classify("x")
        assert client.kwargs is not None
        assert "guardrailConfig" not in client.kwargs


class TestParseVerdict:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("ALERT", True),
            ("alert", True),
            ("CHATTER", False),
            ("  Chatter  ", False),
            ("dunno", None),
            ("", None),
        ],
    )
    def test_parse(self, text, expected):
        assert _parse_llm_verdict(_converse_response(text)) is expected

    def test_parse_malformed_response_none(self):
        assert _parse_llm_verdict({}) is None


class TestLlmFromEnv:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("CLASSIFIER_LLM_ENABLED", raising=False)
        assert llm_classifier_from_env() is None

    def test_enabled_builds_classifier(self, monkeypatch):
        monkeypatch.setenv("CLASSIFIER_LLM_ENABLED", "true")
        assert isinstance(llm_classifier_from_env(), BedrockLlmClassifier)
