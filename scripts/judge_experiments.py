#!/usr/bin/env python3
"""Offline A/B judge CLI — score variant reports for an experiment (issue #26).

Reads the stored variant reports from the experiment-results table, runs the
dual-order LLM judge (:mod:`shared.experiment_judge`) over every complete a/b
pair, persists each :class:`~shared.experiment.Judgement` back as a
``#judgement`` sibling, and prints a per-investigation + aggregate report.

    AWS_PROFILE=nmi-dev python scripts/judge_experiments.py --experiment-id exp-123
    ... --rejudge        # re-score even already-judged investigations
    ... --json           # emit the same data as structured JSON

The judge model defaults to an Opus-class profile; override with
``JUDGE_MODEL_ID``. Runs strictly out-of-band — never on the investigation hot
path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Allow running as a bare script (``python scripts/judge_experiments.py``).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.experiment import ExperimentConfig  # noqa: E402
from shared.experiment_judge import ExperimentJudge  # noqa: E402
from shared.experiment_report import (  # noqa: E402
    JudgedPair,
    render_text,
    summarize,
    to_dict,
)
from shared.experiment_results_store import ExperimentResultsStore  # noqa: E402
from shared.experiment_store import ExperimentStore  # noqa: E402
from shared.time_utils import now_iso  # noqa: E402

logger = logging.getLogger("judge_experiments")


def variant_model_ids(config: ExperimentConfig | None) -> tuple[str | None, ...]:
    """Collect every per-agent model id across both variants (for the bias warning)."""
    if config is None:
        return ()
    ids: list[str | None] = []
    for variant in (config.variant_a, config.variant_b):
        for agent in variant.agents.values():
            if agent.model_id:
                ids.append(agent.model_id)
    return tuple(ids)


async def run(
    experiment_id: str,
    *,
    results_store: ExperimentResultsStore,
    judge: ExperimentJudge,
    model_ids: tuple[str | None, ...] = (),
    rejudge: bool = False,
    as_json: bool = False,
) -> str:
    """Judge every complete pair and render the report. Fail-open per pair."""
    judged: list[JudgedPair] = []
    for variant_a, variant_b in results_store.iter_pairs(experiment_id):
        inv = variant_a.investigation_id
        existing = None if rejudge else results_store.get_judgement(experiment_id, inv)
        if existing is not None:
            judged.append((existing, variant_a, variant_b))
            continue
        try:
            judgement = await judge.judge_pair(
                variant_a,
                variant_b,
                variant_model_ids=model_ids,
                timestamp=now_iso(),
            )
        except Exception:
            logger.exception("Judge failed for investigation %s; skipping", inv)
            continue
        results_store.put_judgement(judgement)
        judged.append((judgement, variant_a, variant_b))

    summary = summarize(experiment_id, judged)
    return json.dumps(to_dict(summary), indent=2) if as_json else render_text(summary)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", required=True, help="Experiment to judge.")
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="Re-score investigations that already have a stored judgement.",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit structured JSON."
    )
    parser.add_argument(
        "--results-table",
        default=None,
        help="Override the experiment-results DynamoDB table name.",
    )
    parser.add_argument(
        "--experiments-table",
        default=None,
        help="Override the experiment-config DynamoDB table name.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)

    results_kwargs = {"table_name": args.results_table} if args.results_table else {}
    config_kwargs = {"table_name": args.experiments_table} if args.experiments_table else {}
    results_store = ExperimentResultsStore(**results_kwargs)
    experiment_store = ExperimentStore(**config_kwargs)

    config = experiment_store.get_experiment(args.experiment_id)
    judge = ExperimentJudge.from_env()

    output = asyncio.run(
        run(
            args.experiment_id,
            results_store=results_store,
            judge=judge,
            model_ids=variant_model_ids(config),
            rejudge=args.rejudge,
            as_json=args.as_json,
        )
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
