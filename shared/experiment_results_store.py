"""DynamoDB-backed store for A/B experiment investigation results."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any, cast

from boto3.dynamodb.conditions import Attr

from shared.dynamo_table import DynamoTable
from shared.experiment import ExperimentResult, Judgement

DEFAULT_TABLE_NAME = "sre-on-call-experiment-results"

# Env var letting a deployment point the store at a results table other than
# the project default — used by the A/B scorecard (#29), where a control-arm
# master deployed as a separate stack (its own ``project_name``) writes into the
# treatment's results table so the judge can pair both variants.
_TABLE_NAME_ENV = "EXPERIMENT_RESULTS_TABLE_NAME"

# Experiment results expire after 30 days.
_TTL_SECONDS = 30 * 86400

# Suffix distinguishing a judgement row from the two variant-result rows that
# share the same experiment/investigation prefix.
_JUDGEMENT_SUFFIX = "judgement"


class ExperimentResultsStore:
    """Stores per-variant investigation results for offline comparison."""

    def __init__(
        self,
        table_name: str | None = None,
        dynamodb_resource: Any = None,
    ) -> None:
        resolved = table_name or os.environ.get(_TABLE_NAME_ENV) or DEFAULT_TABLE_NAME
        # Offline judge-CLI path: fail-closed so a silent DDB error never
        # corrupts a scorecard. Floats marshal to Decimal inside the adapter.
        self._table = DynamoTable(resolved, dynamodb_resource=dynamodb_resource)

    def put_result(self, result: ExperimentResult) -> None:
        """Write an experiment result to DynamoDB."""
        now = int(time.time())
        item: dict[str, Any] = {
            "pk": f"{result.experiment_id}#{result.investigation_id}#{result.variant_id}",
            "experiment_id": result.experiment_id,
            "investigation_id": result.investigation_id,
            "variant_id": result.variant_id,
            "report": result.report,
            "agent_durations": dict(result.agent_durations),
            "total_duration_seconds": result.total_duration_seconds,
            "timestamp": result.timestamp,
            "ttl": now + _TTL_SECONDS,
        }
        # Optional telemetry — omit the attributes entirely when absent so old
        # rows and new rows share one shape and reads can default to ``None``.
        if result.total_cost_usd is not None:
            item["total_cost_usd"] = result.total_cost_usd
        if result.total_tokens is not None:
            item["total_tokens"] = int(result.total_tokens)
        self._table.put(item)

    def get_results(self, experiment_id: str, investigation_id: str) -> list[ExperimentResult]:
        """Fetch both variant results for a given investigation."""

        results = []
        for vid in ("a", "b"):
            pk = f"{experiment_id}#{investigation_id}#{vid}"
            item = self._table.get({"pk": pk})
            if item:
                results.append(_result_from_item(item))
        return results

    def iter_pairs(
        self, experiment_id: str
    ) -> Iterator[tuple[ExperimentResult, ExperimentResult]]:
        """Yield ``(variant_a, variant_b)`` for every investigation with both rows.

        Scans the table for the experiment's variant-result rows (no GSI; v1
        enumerates by scan) and yields only complete pairs — investigations
        missing a variant are skipped so the judge never compares against a hole.
        """
        by_investigation: dict[str, dict[str, ExperimentResult]] = {}
        for item in self._table.scan_all(Attr("experiment_id").eq(experiment_id)):
            vid = item.get("variant_id")
            if vid not in ("a", "b"):
                continue  # judgement rows and anything unexpected
            result = _result_from_item(item)
            by_investigation.setdefault(result.investigation_id, {})[vid] = result

        for variants in by_investigation.values():
            if "a" in variants and "b" in variants:
                yield variants["a"], variants["b"]

    def put_judgement(self, judgement: Judgement) -> None:
        """Persist a judgement as a ``#judgement`` sibling of its result rows.

        Idempotent — re-judging the same investigation overwrites in place.
        """
        now = int(time.time())
        self._table.put({
            "pk": f"{judgement.experiment_id}#{judgement.investigation_id}#{_JUDGEMENT_SUFFIX}",
            "experiment_id": judgement.experiment_id,
            "investigation_id": judgement.investigation_id,
            "record_type": _JUDGEMENT_SUFFIX,
            "overall_winner": judgement.overall_winner,
            "dimension_winners": dict(judgement.dimension_winners),
            "judge_model_id": judgement.judge_model_id,
            "rationale": judgement.rationale,
            "timestamp": judgement.timestamp,
            "ttl": now + _TTL_SECONDS,
        })

    def get_judgement(self, experiment_id: str, investigation_id: str) -> Judgement | None:
        """Fetch the stored judgement for an investigation, or ``None``."""
        pk = f"{experiment_id}#{investigation_id}#{_JUDGEMENT_SUFFIX}"
        item = self._table.get({"pk": pk})
        if not item:
            return None
        d = cast(dict[str, Any], item)
        return Judgement(
            experiment_id=d["experiment_id"],
            investigation_id=d["investigation_id"],
            overall_winner=d["overall_winner"],
            dimension_winners={k: str(v) for k, v in d.get("dimension_winners", {}).items()},
            judge_model_id=d.get("judge_model_id", ""),
            rationale=d.get("rationale", ""),
            timestamp=d.get("timestamp", ""),
        )

def _result_from_item(d: dict[str, Any]) -> ExperimentResult:
    """Reconstruct an :class:`ExperimentResult` from a DynamoDB item."""
    cost = d.get("total_cost_usd")
    tokens = d.get("total_tokens")
    return ExperimentResult(
        experiment_id=d["experiment_id"],
        investigation_id=d["investigation_id"],
        variant_id=d["variant_id"],
        report=d["report"],
        agent_durations={k: float(v) for k, v in d.get("agent_durations", {}).items()},
        total_duration_seconds=float(d.get("total_duration_seconds", 0)),
        timestamp=d.get("timestamp", ""),
        total_cost_usd=float(cost) if cost is not None else None,
        total_tokens=int(tokens) if tokens is not None else None,
    )
