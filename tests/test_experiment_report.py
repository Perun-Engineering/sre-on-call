"""Tests for the A/B judge report aggregation + rendering (issue #26)."""

from __future__ import annotations

import pytest

from shared.experiment import ExperimentResult, Judgement
from shared.experiment_report import JudgedPair, render_text, summarize, to_dict


def _result(
    variant_id: str,
    investigation_id: str,
    *,
    cost: float | None = None,
    latency: float = 0.0,
) -> ExperimentResult:
    return ExperimentResult(
        experiment_id="exp-1",
        investigation_id=investigation_id,
        variant_id=variant_id,
        report="r",
        total_duration_seconds=latency,
        total_cost_usd=cost,
    )


def _judgement(
    investigation_id: str,
    overall: str,
    dims: dict[str, str] | None = None,
) -> Judgement:
    return Judgement(
        experiment_id="exp-1",
        investigation_id=investigation_id,
        overall_winner=overall,
        dimension_winners=dims
        or {"coverage": overall, "severity": overall, "actionability": overall, "noise": overall},
        judge_model_id="judge",
    )


def _pair(
    investigation_id: str,
    overall: str,
    *,
    cost_a: float | None = None,
    cost_b: float | None = None,
    latency_a: float = 0.0,
    latency_b: float = 0.0,
    dims: dict[str, str] | None = None,
) -> JudgedPair:
    return (
        _judgement(investigation_id, overall, dims),
        _result("a", investigation_id, cost=cost_a, latency=latency_a),
        _result("b", investigation_id, cost=cost_b, latency=latency_b),
    )


class TestPerInvestigation:
    def test_cost_ratio_b_over_a(self):
        summary = summarize("exp-1", [_pair("inv-1", "b", cost_a=0.10, cost_b=0.25)])
        assert summary.rows[0].cost_ratio_b_over_a == 2.5

    def test_cost_ratio_none_when_cost_missing(self):
        summary = summarize("exp-1", [_pair("inv-1", "b", cost_a=None, cost_b=0.25)])
        assert summary.rows[0].cost_ratio_b_over_a is None

    def test_cost_ratio_none_when_a_zero(self):
        summary = summarize("exp-1", [_pair("inv-1", "b", cost_a=0.0, cost_b=0.25)])
        assert summary.rows[0].cost_ratio_b_over_a is None

    def test_latency_delta_b_minus_a(self):
        summary = summarize("exp-1", [_pair("inv-1", "a", latency_a=4.0, latency_b=6.5)])
        assert summary.rows[0].latency_delta_seconds == 2.5

    def test_carries_dimension_winners(self):
        dims = {"coverage": "a", "severity": "b", "actionability": "tie", "noise": "a"}
        summary = summarize("exp-1", [_pair("inv-1", "a", dims=dims)])
        assert summary.rows[0].dimension_winners == dims


class TestAggregate:
    def test_dimension_tally(self):
        pairs = [
            _pair("inv-1", "a", dims={"coverage": "a", "severity": "a", "actionability": "a", "noise": "a"}),
            _pair("inv-2", "b", dims={"coverage": "b", "severity": "a", "actionability": "tie", "noise": "b"}),
        ]
        agg = summarize("exp-1", pairs).aggregate
        assert agg is not None
        assert (agg.dimensions["coverage"].a_wins, agg.dimensions["coverage"].b_wins) == (1, 1)
        assert agg.dimensions["severity"].a_wins == 2
        assert agg.dimensions["actionability"].ties == 1

    def test_overall_win_rate(self):
        pairs = [_pair("inv-1", "b"), _pair("inv-2", "b"), _pair("inv-3", "a"), _pair("inv-4", "tie")]
        agg = summarize("exp-1", pairs).aggregate
        assert agg is not None
        assert agg.b_win_rate == 0.5
        assert agg.a_win_rate == 0.25
        assert (agg.overall.a_wins, agg.overall.b_wins, agg.overall.ties) == (1, 2, 1)

    def test_mean_cost_and_latency(self):
        pairs = [
            _pair("inv-1", "a", cost_a=0.10, cost_b=0.20, latency_a=2.0, latency_b=4.0),
            _pair("inv-2", "b", cost_a=0.30, cost_b=0.40, latency_a=6.0, latency_b=8.0),
        ]
        agg = summarize("exp-1", pairs).aggregate
        assert agg is not None
        assert agg.mean_cost_a == pytest.approx(0.20)
        assert agg.mean_cost_b == pytest.approx(0.30)
        assert agg.mean_latency_a == pytest.approx(4.0)
        assert agg.mean_latency_b == pytest.approx(6.0)

    def test_mean_cost_none_when_no_costs(self):
        agg = summarize("exp-1", [_pair("inv-1", "a")]).aggregate
        assert agg is not None
        assert agg.mean_cost_a is None

    def test_headline_names_leader(self):
        pairs = [_pair("inv-1", "b"), _pair("inv-2", "b"), _pair("inv-3", "a")]
        agg = summarize("exp-1", pairs).aggregate
        assert agg is not None
        assert "Variant B wins 2/3" in agg.headline

    def test_empty_summary(self):
        summary = summarize("exp-1", [])
        assert summary.aggregate is not None
        assert summary.aggregate.investigations == 0
        assert "No judged" in summary.aggregate.headline


class TestRendering:
    def test_text_contains_key_lines(self):
        pairs = [_pair("inv-1", "b", cost_a=0.10, cost_b=0.25, latency_a=2.0, latency_b=5.0)]
        text = render_text(summarize("exp-1", pairs))
        assert "Experiment exp-1" in text
        assert "inv-1" in text
        assert "winner=B" in text
        assert "2.50×" in text
        assert "+3.0s" in text

    def test_text_empty(self):
        text = render_text(summarize("exp-1", []))
        assert "No judged investigations." in text

    def test_to_dict_roundtrips_structure(self):
        pairs = [_pair("inv-1", "b", cost_a=0.10, cost_b=0.25)]
        d = to_dict(summarize("exp-1", pairs))
        assert d["experiment_id"] == "exp-1"
        assert d["rows"][0]["overall_winner"] == "b"
        assert d["aggregate"]["dimensions"]["coverage"]["b_wins"] == 1
