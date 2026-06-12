#!/usr/bin/env python3
"""Define + activate an A/B experiment over two master pipelines (issue #58).

The intake Lambda fans every alert out to both ``master_endpoint`` ARNs of the
single ``active`` experiment, tagging each run's ``variant_id`` so the
experiment-results store can pair them. This CLI writes that record so an
operator doesn't hand-assemble the DynamoDB item.

For the #58 scorecard the two variants are two *deployments*:

    variant a (control)   = a pre-#58 master runtime  (Haiku, single-pass)
    variant b (treatment) = the #58 master runtime     (Sonnet, bounded loop)

Only ``master_endpoint`` matters for routing — the per-variant agent behaviour
lives in each deployed runtime's config, not in this record.

    AWS_PROFILE=<dev> python scripts/define_experiment.py \\
        --experiment-id sonnet-loop-58 \\
        --name "haiku-oneshot-vs-sonnet-loop" \\
        --control-arn  arn:aws:bedrock-agentcore:...:runtime/master-control \\
        --treatment-arn arn:aws:bedrock-agentcore:...:runtime/master-58 \\
        --table sre-on-call-experiments

Then replay alerts (see docs/scorecard-runbook.md) and score with
``scripts/judge_experiments.py --experiment-id sonnet-loop-58``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.experiment import ExperimentConfig, PipelineVariant  # noqa: E402
from shared.experiment_store import ExperimentStore  # noqa: E402
from shared.time_utils import now_iso  # noqa: E402


def build_config(
    *,
    experiment_id: str,
    name: str,
    control_arn: str,
    treatment_arn: str,
    control_label: str,
    treatment_label: str,
    status: str,
) -> ExperimentConfig:
    """Assemble the two-variant :class:`ExperimentConfig` (pure; unit-tested)."""
    ts = now_iso()
    return ExperimentConfig(
        experiment_id=experiment_id,
        name=name,
        status=status,
        variant_a=PipelineVariant(
            variant_id="a", label=control_label, master_endpoint=control_arn,
        ),
        variant_b=PipelineVariant(
            variant_id="b", label=treatment_label, master_endpoint=treatment_arn,
        ),
        created_at=ts,
        updated_at=ts,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--control-arn", required=True, help="variant a master runtime ARN")
    p.add_argument("--treatment-arn", required=True, help="variant b master runtime ARN")
    p.add_argument("--control-label", default="haiku-oneshot")
    p.add_argument("--treatment-label", default="sonnet-bounded-loop")
    p.add_argument(
        "--status", default="active", choices=("active", "paused", "completed")
    )
    p.add_argument("--table", default=None, help="experiments table name")
    p.add_argument(
        "--dry-run", action="store_true", help="print the record, don't write it"
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = build_config(
        experiment_id=args.experiment_id,
        name=args.name,
        control_arn=args.control_arn,
        treatment_arn=args.treatment_arn,
        control_label=args.control_label,
        treatment_label=args.treatment_label,
        status=args.status,
    )

    if args.dry_run:
        print(f"[dry-run] would write active experiment {config.experiment_id!r}:")
        print(f"  a ({config.variant_a.label}) -> {config.variant_a.master_endpoint}")
        print(f"  b ({config.variant_b.label}) -> {config.variant_b.master_endpoint}")
        return 0

    store = ExperimentStore(table_name=args.table) if args.table else ExperimentStore()
    store.put_experiment(config)
    print(f"Wrote {config.status} experiment {config.experiment_id!r} to the store.")
    print("Now replay alerts, then: scripts/judge_experiments.py "
          f"--experiment-id {config.experiment_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
