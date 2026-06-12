"""Tests for the A/B experiment-definition helper (issue #58)."""

from __future__ import annotations

from scripts.define_experiment import build_config, main


def test_build_config_assembles_two_variants() -> None:
    cfg = build_config(
        experiment_id="sonnet-loop-58",
        name="haiku-vs-sonnet",
        control_arn="arn:control",
        treatment_arn="arn:treatment",
        control_label="haiku-oneshot",
        treatment_label="sonnet-bounded-loop",
        status="active",
    )
    assert cfg.status == "active"
    assert cfg.variant_a.variant_id == "a"
    assert cfg.variant_a.master_endpoint == "arn:control"
    assert cfg.variant_b.variant_id == "b"
    assert cfg.variant_b.master_endpoint == "arn:treatment"
    assert cfg.created_at == cfg.updated_at  # stamped once


def test_main_dry_run_writes_nothing(capsys) -> None:
    rc = main([
        "--experiment-id", "e1",
        "--name", "n",
        "--control-arn", "arn:c",
        "--treatment-arn", "arn:t",
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "arn:c" in out and "arn:t" in out
