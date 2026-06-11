"""Render and aggregate A/B judge results into a comparison report (issue #26).

Pure transformation: judged pairs in, structured + human-readable views out.
No DynamoDB, no Bedrock — fully unit-testable. The CLI
(``scripts/judge_experiments.py``) wires the stores and the judge to this.

Two views:
- per-investigation rows (winner, per-dimension winners, cost ratio, latency
  delta), and
- an aggregate decision view (per-dimension a/b/tie tallies, overall win rate,
  mean cost & latency per variant, and a one-line headline).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from shared.experiment import JUDGEMENT_DIMENSIONS, ExperimentResult, Judgement

# A judgement paired with the two variant results it scored.
JudgedPair = tuple[Judgement, ExperimentResult, ExperimentResult]


@dataclass
class InvestigationRow:
    """Per-investigation comparison line."""

    investigation_id: str
    overall_winner: str  # "a" | "b" | "tie"
    dimension_winners: dict[str, str]
    cost_a: float | None
    cost_b: float | None
    latency_a: float
    latency_b: float
    cost_ratio_b_over_a: float | None  # how much more B cost than A (×)
    latency_delta_seconds: float  # B − A (positive => B slower)


@dataclass
class Tally:
    a_wins: int = 0
    b_wins: int = 0
    ties: int = 0

    def record(self, winner: str) -> None:
        if winner == "a":
            self.a_wins += 1
        elif winner == "b":
            self.b_wins += 1
        else:
            self.ties += 1


@dataclass
class Aggregate:
    """Experiment-wide decision view."""

    investigations: int
    overall: Tally
    dimensions: dict[str, Tally]
    a_win_rate: float
    b_win_rate: float
    mean_cost_a: float | None
    mean_cost_b: float | None
    mean_latency_a: float
    mean_latency_b: float
    headline: str


@dataclass
class ExperimentSummary:
    experiment_id: str
    rows: list[InvestigationRow] = field(default_factory=list)
    aggregate: Aggregate | None = None


def _row(judgement: Judgement, a: ExperimentResult, b: ExperimentResult) -> InvestigationRow:
    cost_ratio = None
    if a.total_cost_usd not in (None, 0) and b.total_cost_usd is not None:
        cost_ratio = b.total_cost_usd / a.total_cost_usd  # type: ignore[operator]
    return InvestigationRow(
        investigation_id=judgement.investigation_id,
        overall_winner=judgement.overall_winner,
        dimension_winners=dict(judgement.dimension_winners),
        cost_a=a.total_cost_usd,
        cost_b=b.total_cost_usd,
        latency_a=a.total_duration_seconds,
        latency_b=b.total_duration_seconds,
        cost_ratio_b_over_a=cost_ratio,
        latency_delta_seconds=b.total_duration_seconds - a.total_duration_seconds,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(experiment_id: str, pairs: list[JudgedPair]) -> ExperimentSummary:
    """Build the per-investigation rows and the aggregate decision view."""
    rows = [_row(j, a, b) for j, a, b in pairs]

    overall = Tally()
    dims = {dim: Tally() for dim in JUDGEMENT_DIMENSIONS}
    for row in rows:
        overall.record(row.overall_winner)
        for dim in JUDGEMENT_DIMENSIONS:
            dims[dim].record(row.dimension_winners.get(dim, "tie"))

    n = len(rows)
    a_win_rate = overall.a_wins / n if n else 0.0
    b_win_rate = overall.b_wins / n if n else 0.0
    mean_cost_a = _mean([r.cost_a for r in rows if r.cost_a is not None])
    mean_cost_b = _mean([r.cost_b for r in rows if r.cost_b is not None])
    mean_latency_a = _mean([r.latency_a for r in rows]) or 0.0
    mean_latency_b = _mean([r.latency_b for r in rows]) or 0.0

    aggregate = Aggregate(
        investigations=n,
        overall=overall,
        dimensions=dims,
        a_win_rate=a_win_rate,
        b_win_rate=b_win_rate,
        mean_cost_a=mean_cost_a,
        mean_cost_b=mean_cost_b,
        mean_latency_a=mean_latency_a,
        mean_latency_b=mean_latency_b,
        headline=_headline(overall, n),
    )
    return ExperimentSummary(experiment_id=experiment_id, rows=rows, aggregate=aggregate)


def _headline(overall: Tally, n: int) -> str:
    if n == 0:
        return "No judged investigations."
    if overall.a_wins > overall.b_wins:
        leader, wins = "A", overall.a_wins
    elif overall.b_wins > overall.a_wins:
        leader, wins = "B", overall.b_wins
    else:
        return (
            f"No clear winner across {n} investigations "
            f"(A {overall.a_wins}, B {overall.b_wins}, ties {overall.ties})."
        )
    return (
        f"Variant {leader} wins {wins}/{n} investigations "
        f"(A {overall.a_wins}, B {overall.b_wins}, ties {overall.ties})."
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_winner(winner: str) -> str:
    return {"a": "A", "b": "B", "tie": "tie"}.get(winner, winner)


def _fmt_cost_ratio(ratio: float | None) -> str:
    return f"{ratio:.2f}×" if ratio is not None else "n/a"


def _fmt_delta(seconds: float) -> str:
    return f"{seconds:+.1f}s"


def render_text(summary: ExperimentSummary) -> str:
    """Render the human-readable per-investigation + aggregate report."""
    lines = [f"Experiment {summary.experiment_id}", ""]

    if not summary.rows:
        lines.append("No judged investigations.")
        return "\n".join(lines)

    lines.append("Per-investigation:")
    for row in summary.rows:
        dims = " ".join(
            f"{dim}={_fmt_winner(row.dimension_winners.get(dim, 'tie'))}"
            for dim in JUDGEMENT_DIMENSIONS
        )
        lines.append(
            f"  {row.investigation_id}: winner={_fmt_winner(row.overall_winner)} "
            f"[{dims}] cost(B/A)={_fmt_cost_ratio(row.cost_ratio_b_over_a)} "
            f"latency(B−A)={_fmt_delta(row.latency_delta_seconds)}"
        )

    agg = summary.aggregate
    assert agg is not None  # rows present => aggregate built
    lines += ["", "Aggregate:", f"  {agg.headline}"]
    for dim in JUDGEMENT_DIMENSIONS:
        t = agg.dimensions[dim]
        lines.append(f"  {dim}: A {t.a_wins} / B {t.b_wins} / ties {t.ties}")
    lines.append(
        f"  overall win rate: A {agg.a_win_rate:.0%} / B {agg.b_win_rate:.0%}"
    )
    lines.append(
        f"  mean cost: A {_fmt_cost(agg.mean_cost_a)} / B {_fmt_cost(agg.mean_cost_b)}"
    )
    lines.append(
        f"  mean latency: A {agg.mean_latency_a:.1f}s / B {agg.mean_latency_b:.1f}s"
    )
    return "\n".join(lines)


def _fmt_cost(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "n/a"


def to_dict(summary: ExperimentSummary) -> dict:
    """Structured form of the report — same data as ``render_text``, for ``--json``."""
    return asdict(summary)
