"""Round-trip test for scripts/synthetic_slack_webhook.py — its signing
must match the production verifier in lambda_adapter/slack/signature.py.

If this test passes, a payload signed by the script will pass signature
verification when the Lambda processes it.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

from shared.platforms.slack import verify_slack_signature


def _load_script_module():
    path = pathlib.Path(__file__).resolve().parent.parent.parent / "scripts" / "synthetic_slack_webhook.py"
    spec = importlib.util.spec_from_file_location("synthetic_slack_webhook", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["synthetic_slack_webhook"] = module
    spec.loader.exec_module(module)
    return module


def test_signature_round_trips_through_production_verifier():
    script = _load_script_module()
    secret = "deadbeefdeadbeefdeadbeefdeadbeef"
    body = '{"type":"event_callback","team_id":"T1"}'
    # Production verifier rejects requests older than 5 minutes, so use now.
    timestamp = str(int(time.time()))

    signature = script.sign(secret, timestamp, body)

    assert signature.startswith("v0=")
    assert verify_slack_signature(
        body=body,
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
    ) is True


def test_tampered_body_fails_verification():
    script = _load_script_module()
    secret = "deadbeefdeadbeefdeadbeefdeadbeef"
    body = '{"original":"body"}'
    timestamp = str(int(time.time()))

    signature = script.sign(secret, timestamp, body)

    assert verify_slack_signature(
        body='{"tampered":"body"}',
        timestamp=timestamp,
        signature=signature,
        signing_secret=secret,
    ) is False
