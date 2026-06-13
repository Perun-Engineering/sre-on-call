"""Alert classification gate for the intake pipeline.

Every ``app_mention`` reaching the bot currently triggers a full investigation
fan-out — a casual "@bot thanks!" launches every specialized agent. This module
decides, before dispatch, whether a mention is actually an *alert* worth
investigating.

Two tiers, cheapest first:

* **Tier 1 — heuristics** (:func:`classify_heuristic`): a pure, deterministic
  scan for alert-shaped markers (severity keywords, Alertmanager/Grafana
  formatting, dashboard/console links) and, on the other side, obvious
  conversational chatter. Confident verdicts short-circuit here.
* **Tier 2 — LLM** (:class:`BedrockLlmClassifier`): for messages Tier 1 cannot
  call, an optional single Bedrock Converse turn (Haiku by default) decides.
  Gated behind ``CLASSIFIER_LLM_ENABLED`` and fully fail-open.

The cardinal rule is **fail toward investigating**: when neither tier can
confidently say "not an alert" (ambiguous Tier 1, LLM disabled or erroring),
:func:`classify_alert` defaults to *alert* so a real page is never silently
swallowed. The gate only suppresses messages it is confident are chatter.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

from shared.env import truthy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Classification:
    """Verdict for one intake message.

    ``is_alert`` drives the gate: ``True`` dispatches the investigation,
    ``False`` suppresses it. ``tier`` records which stage decided
    (``"override"`` | ``"heuristic"`` | ``"llm"`` | ``"default"``) and
    ``reason`` is a short human string for logs and trace tuning.
    """

    is_alert: bool
    tier: str
    reason: str


# ---------------------------------------------------------------------------
# Tier 1 — heuristics
# ---------------------------------------------------------------------------

# Forces an investigation regardless of any other signal — the documented
# manual override ("@bot investigate this please").
_OVERRIDE_PATTERN = re.compile(r"\binvestigate\b", re.IGNORECASE)

# Words that, on their own, mark a message as alert-shaped. Matched as whole
# words (case-insensitive) so "erroring" or "downtown" don't trip "error"/"down".
_SEVERITY_KEYWORDS = (
    "alert",
    "alarm",
    "critical",
    "crit",
    "warning",
    "error",
    "errors",
    "fatal",
    "exception",
    "outage",
    "incident",
    "degraded",
    "down",
    "unavailable",
    "unreachable",
    "timeout",
    "timed out",
    "failing",
    "failed",
    "failure",
    "firing",
    "paging",
    "pagerduty",
    "saturation",
    "throttled",
    "oomkilled",
    "crashloopbackoff",
    "5xx",
    "latency",
    "spike",
    "breach",
    "breached",
    "threshold",
    "sev1",
    "sev2",
    "sev3",
    "p1",
    "p2",
    "p3",
)

_SEVERITY_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(k) for k in _SEVERITY_KEYWORDS) + r")(?!\w)",
    re.IGNORECASE,
)

# Substrings that betray machine-generated alert formatting (Alertmanager,
# Grafana, Prometheus, CloudWatch, Datadog, Opsgenie, …). Plain substring
# matches — these tokens are unambiguous when present.
_FORMATTING_MARKERS = (
    "[firing",
    "[resolved",
    "alertname=",
    "labels:",
    "annotations:",
    "severity=",
    "severity:",
    "namespace=",
    "alertmanager",
    "grafana",
    "prometheus",
    "cloudwatch",
    "datadog",
    "opsgenie",
    "pagerduty",
    "newrelic",
    "sentry",
    ":fire:",
    "\U0001f525",  # 🔥
    "\U0001f6a8",  # 🚨
    "⚠",  # ⚠
)

# Links to dashboards/consoles are a strong alert tell even without keywords.
_DASHBOARD_LINK_PATTERN = re.compile(
    r"https?://[^\s|>]*"
    r"(?:grafana|kibana|datadoghq|console\.aws|cloudwatch|"
    r"dashboards?|\.pagerduty\.|opsgenie)",
    re.IGNORECASE,
)

# Confident chatter: short greetings / acknowledgements with no alert markers.
# Anchored on the whole (marker-stripped) message so "thanks for the alert"
# does NOT match (it still carries "alert").
_CHATTER_PATTERN = re.compile(
    r"^(?:"
    r"thanks?|thank\s+you|thx|ty|cheers|nice|great|cool|awesome|"
    r"lol|haha|ok|okay|kk|got\s+it|gotcha|"
    r"hi|hey|hello|yo|sup|gm|good\s+morning|good\s+night|"
    r"test|testing|ping|pong|hello\s+world|are\s+you\s+(?:there|alive|up)|"
    r"who\s+are\s+you|what\s+can\s+you\s+do|help"
    r")[\s!.?]*$",
    re.IGNORECASE,
)

# Below this length, a marker-free message is treated as chatter rather than
# deferred to Tier 2 (a bare "@bot" or an emoji reaction is not an alert).
_SHORT_CHATTER_MAX_WORDS = 4


def _strip_mentions(text: str) -> str:
    """Remove Slack/Discord mention tokens so they don't skew length or matching.

    Slack renders mentions as ``<@U123>``/``<#C123|name>``; Discord as
    ``<@123>``/``<#123>``. Bare ``@name`` handles are left as-is (harmless).
    """
    return re.sub(r"<[@#!&][^>]*>", " ", text)


def classify_heuristic(alert_text: str) -> Classification | None:
    """Tier 1: classify *alert_text* by cheap deterministic heuristics.

    Returns a :class:`Classification` only when confident:

    * ``is_alert=True``  — manual override, severity keyword, alert formatting,
      or a dashboard/console link is present.
    * ``is_alert=False`` — the message is empty or unmistakable chatter.

    Returns ``None`` when the message is ambiguous (no alert markers but not
    obviously chatter either), leaving the verdict to Tier 2 or the caller's
    fail-open default.
    """
    stripped = _strip_mentions(alert_text or "")
    normalized = stripped.strip()

    if _OVERRIDE_PATTERN.search(normalized):
        return Classification(True, "override", "manual 'investigate' override")

    if not normalized:
        return Classification(False, "heuristic", "empty message")

    lowered = normalized.lower()

    if _SEVERITY_PATTERN.search(normalized):
        return Classification(True, "heuristic", "severity keyword")
    if any(marker in lowered for marker in _FORMATTING_MARKERS):
        return Classification(True, "heuristic", "alert formatting marker")
    if _DASHBOARD_LINK_PATTERN.search(normalized):
        return Classification(True, "heuristic", "dashboard/console link")

    if _CHATTER_PATTERN.match(normalized):
        return Classification(False, "heuristic", "conversational chatter")
    if len(normalized.split()) <= _SHORT_CHATTER_MAX_WORDS:
        return Classification(False, "heuristic", "short marker-free message")

    return None


# ---------------------------------------------------------------------------
# Tier 2 — LLM
# ---------------------------------------------------------------------------


class LlmClassifier(Protocol):
    """Seam over a single LLM turn that judges ambiguous messages.

    ``classify`` returns ``True`` (alert), ``False`` (not an alert), or
    ``None`` when the model is unavailable or the answer can't be parsed —
    ``None`` means "no opinion", and the caller falls back to investigating.
    """

    def classify(self, alert_text: str) -> bool | None: ...  # pragma: no cover


_LLM_SYSTEM_PROMPT = (
    "You are the intake filter for an automated incident-investigation bot. "
    "You receive a single chat message that mentioned the bot. Decide whether "
    "it is an operational ALERT that warrants launching an investigation "
    "(monitoring alarms, error reports, outage notices, on-call pages, "
    "infrastructure problems) or ordinary CHATTER (greetings, thanks, jokes, "
    "questions about the bot, idle conversation).\n"
    "Respond with exactly one word: ALERT or CHATTER. No punctuation, no "
    "explanation."
)

# Default Bedrock model for the classifier. Resolution order at call time:
# CLASSIFIER_MODEL_ID -> MODEL_ID -> this constant.
_DEFAULT_CLASSIFIER_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


class BedrockLlmClassifier:
    """:class:`LlmClassifier` backed by a single Bedrock Converse turn.

    Synchronous on purpose — the intake pipeline runs in a synchronous Lambda
    handler, so this uses a lazily-created ``bedrock-runtime`` boto3 client and
    a one-shot Converse call rather than the async Strands path used elsewhere.

    Fully fail-open: any missing dependency, client error, throttle, or
    unparseable response yields ``None`` so the gate falls back to
    investigating.
    """

    def __init__(self, *, model_id: str | None = None, client=None) -> None:
        self._model_id = (
            model_id
            or os.environ.get("CLASSIFIER_MODEL_ID")
            or os.environ.get("MODEL_ID")
            or _DEFAULT_CLASSIFIER_MODEL_ID
        )
        self._client = client

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime")
        return self._client

    def classify(self, alert_text: str) -> bool | None:
        try:
            kwargs: dict = dict(
                modelId=self._model_id,
                system=[{"text": _LLM_SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": alert_text}]}],
                inferenceConfig={"maxTokens": 5, "temperature": 0.0},
            )
            # This is the one component reading untrusted chat text. The Strands
            # tier is guardrailed via ``_resolve_model``; mirror that here when
            # ``BEDROCK_GUARDRAIL_ID`` is set so the raw Converse turn does not
            # bypass prompt-attack / content filtering. Unset → unchanged.
            guardrail_id = os.environ.get("BEDROCK_GUARDRAIL_ID") or None
            if guardrail_id:
                kwargs["guardrailConfig"] = {
                    "guardrailIdentifier": guardrail_id,
                    "guardrailVersion": os.environ.get("BEDROCK_GUARDRAIL_VERSION")
                    or "DRAFT",
                }
            response = self._get_client().converse(**kwargs)
            return _parse_llm_verdict(response)
        except Exception:
            logger.warning(
                "LLM classifier call failed; deferring to fail-open default.",
                exc_info=True,
            )
            return None


def _parse_llm_verdict(response: dict) -> bool | None:
    """Extract a boolean verdict from a Bedrock Converse response, or ``None``."""
    try:
        blocks = response["output"]["message"]["content"]
        text = " ".join(b.get("text", "") for b in blocks).strip().upper()
    except (KeyError, TypeError, AttributeError):
        return None
    if "ALERT" in text:
        return True
    if "CHATTER" in text:
        return False
    return None


def llm_classifier_from_env() -> LlmClassifier | None:
    """Build the Tier 2 classifier when ``CLASSIFIER_LLM_ENABLED`` is set.

    Returns ``None`` when disabled (the default), so Tier 1 + the fail-open
    default fully govern classification and no Bedrock call is ever made.
    """
    if not truthy(os.environ.get("CLASSIFIER_LLM_ENABLED")):
        return None
    return BedrockLlmClassifier()


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------


def classify_alert(
    alert_text: str, *, llm: LlmClassifier | None = None
) -> Classification:
    """Classify *alert_text*, combining Tier 1 heuristics with an optional Tier 2 LLM.

    Tier 1 verdicts win outright. On an ambiguous Tier 1 result the *llm* (when
    provided) is consulted; a ``None`` from the LLM, or no LLM at all, falls
    back to **alert** so a genuine page is never dropped.
    """
    heuristic = classify_heuristic(alert_text)
    if heuristic is not None:
        return heuristic

    if llm is not None:
        verdict = llm.classify(alert_text)
        if verdict is not None:
            return Classification(
                verdict, "llm", "alert" if verdict else "not an alert"
            )

    return Classification(True, "default", "ambiguous; defaulting to investigate")
