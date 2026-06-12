"""DynamoDB-backed store for A/B experiment configurations."""

from __future__ import annotations

from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

from shared.experiment import (
    AgentVariantConfig,
    ExperimentConfig,
    PipelineVariant,
)

DEFAULT_TABLE_NAME = "sre-on-call-experiments"


class ExperimentStore:
    """CRUD operations for experiment configs in DynamoDB."""

    def __init__(
        self,
        table_name: str = DEFAULT_TABLE_NAME,
        dynamodb_resource: Any = None,
    ) -> None:
        resource: Any = dynamodb_resource or boto3.resource("dynamodb")
        self._table = resource.Table(table_name)

    def get_active_experiment(self) -> ExperimentConfig | None:
        """Return the single active experiment, or None."""
        resp = self._table.scan(
            FilterExpression=Attr("status").eq("active"),
            Limit=1,
        )
        items = resp.get("Items", [])
        return _item_to_config(items[0]) if items else None

    def get_experiment(self, experiment_id: str) -> ExperimentConfig | None:
        """Fetch a single experiment by ID."""
        resp = self._table.get_item(Key={"pk": f"EXPERIMENT#{experiment_id}"})
        item = resp.get("Item")
        return _item_to_config(item) if item else None

    def put_experiment(self, config: ExperimentConfig) -> None:
        """Write an experiment config to DynamoDB."""
        self._table.put_item(Item=_config_to_item(config))


def _variant_to_dict(v: PipelineVariant) -> dict:
    return {
        "variant_id": v.variant_id,
        "label": v.label,
        "master_endpoint": v.master_endpoint,
        "agents": {
            k: {"endpoint": a.endpoint, "model_id": a.model_id or "", "system_prompt": a.system_prompt or ""}
            for k, a in v.agents.items()
        },
    }


def _dict_to_variant(d: dict) -> PipelineVariant:
    agents = {}
    for k, a in d.get("agents", {}).items():
        agents[k] = AgentVariantConfig(
            endpoint=a["endpoint"],
            model_id=a.get("model_id") or None,
            system_prompt=a.get("system_prompt") or None,
        )
    return PipelineVariant(
        variant_id=d["variant_id"],
        label=d["label"],
        master_endpoint=d["master_endpoint"],
        agents=agents,
    )


def _config_to_item(c: ExperimentConfig) -> dict:
    return {
        "pk": f"EXPERIMENT#{c.experiment_id}",
        "experiment_id": c.experiment_id,
        "name": c.name,
        "status": c.status,
        "variant_a": _variant_to_dict(c.variant_a),
        "variant_b": _variant_to_dict(c.variant_b),
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _item_to_config(item: dict) -> ExperimentConfig:
    return ExperimentConfig(
        experiment_id=item["experiment_id"],
        name=item["name"],
        status=item["status"],
        variant_a=_dict_to_variant(item["variant_a"]),
        variant_b=_dict_to_variant(item["variant_b"]),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
    )
