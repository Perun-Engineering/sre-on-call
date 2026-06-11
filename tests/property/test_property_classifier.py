"""Property-based tests for the intake alert classifier.

The invariants that matter for a *gate*:

* It must never raise on arbitrary input (any chat string is fair game).
* The fail-open contract: without an LLM, a message is only suppressed when
  Tier 1 is confident it is chatter — every other outcome dispatches.
* Strong alert markers (severity keyword, the override keyword) always win.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from lambda_adapter.classifier import (
    _SEVERITY_KEYWORDS,
    classify_alert,
    classify_heuristic,
)

# Arbitrary unicode text, including control chars and emoji, to fuzz the regexes.
any_text = st.text(max_size=400)


@given(any_text)
@settings(max_examples=300)
def test_classify_heuristic_never_raises(text):
    classify_heuristic(text)  # must not raise


@given(any_text)
@settings(max_examples=300)
def test_classify_alert_never_raises_and_returns_bool(text):
    result = classify_alert(text)
    assert isinstance(result.is_alert, bool)
    assert result.tier in ("override", "heuristic", "llm", "default")


@given(any_text)
@settings(max_examples=300)
def test_classify_alert_without_llm_only_suppresses_confident_chatter(text):
    """A suppressed (non-alert) verdict must come from a confident Tier 1 call."""
    result = classify_alert(text)
    if not result.is_alert:
        heuristic = classify_heuristic(text)
        assert heuristic is not None and heuristic.is_alert is False


@given(
    st.sampled_from(_SEVERITY_KEYWORDS),
    st.text(alphabet=st.characters(whitelist_categories=("L", "Zs")), max_size=40),
)
@settings(max_examples=200)
def test_severity_keyword_surrounded_by_spaces_is_alert(keyword, filler):
    # A severity keyword as a standalone token always yields an alert.
    text = f"{filler} {keyword} {filler}".strip()
    result = classify_heuristic(text)
    assert result is not None and result.is_alert is True


@given(st.text(max_size=200))
@settings(max_examples=200)
def test_investigate_override_always_alert(prefix):
    result = classify_heuristic(f"{prefix} investigate")
    assert result is not None and result.is_alert is True
