"""DynamoDB-backed store for A/B experiment investigation results."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, cast

import boto3

from shared.experiment import ExperimentResult

DEFAULT_TABLE_NAME = "sre-on-call-experiment-results"

# Experiment results expire after 30 days.
_TTL_SECONDS = 30 * 86400


class ExperimentResultsStore:
    """Stores per-variant investigation results for offline comparison."""

    def __init__(
        self,
        table_name: str = DEFAULT_TABLE_NAME,
        dynamodb_resource: Any = None,
    ) -> None:
        resource = dynamodb_resource or boto3.resource("dynamodb")
        self._table = resource.Table(table_name)

    def put_result(self, result: ExperimentResult) -> None:
        """Write an experiment result to DynamoDB."""
        now = int(time.time())
        self._table.put_item(Item={
            "pk": f"{result.experiment_id}#{result.investigation_id}#{result.variant_id}",
            "experiment_id": result.experiment_id,
            "investigation_id": result.investigation_id,
            "variant_id": result.variant_id,
            "report": result.report,
            "agent_durations": {k: Decimal(str(v)) for k, v in result.agent_durations.items()},
            "total_duration_seconds": Decimal(str(result.total_duration_seconds)),
            "timestamp": result.timestamp,
            "ttl": now + _TTL_SECONDS,
        })

    def get_results(self, experiment_id: str, investigation_id: str) -> list[ExperimentResult]:
        """Fetch both variant results for a given investigation."""
        from boto3.dynamodb.conditions import Key

        results = []
        for vid in ("a", "b"):
            pk = f"{experiment_id}#{investigation_id}#{vid}"
            resp = self._table.get_item(Key={"pk": pk})
            item = resp.get("Item")
            if item:
                d = cast(dict[str, Any], item)
                results.append(ExperimentResult(
                    experiment_id=d["experiment_id"],
                    investigation_id=d["investigation_id"],
                    variant_id=d["variant_id"],
                    report=d["report"],
                    agent_durations={k: float(v) for k, v in d.get("agent_durations", {}).items()},
                    total_duration_seconds=float(d.get("total_duration_seconds", 0)),
                    timestamp=d.get("timestamp", ""),
                ))
        return results
