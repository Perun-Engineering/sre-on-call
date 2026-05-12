"""Resolve secret references to their plaintext values.

Env vars in this project may carry either a literal value (for local dev /
tests) or a Secrets Manager ARN (when set by Terraform in AWS). Callers
should funnel both forms through ``resolve_secret`` rather than reading
``os.environ`` directly.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import boto3

_SECRETS_CACHE: dict[str, str] = {}
_CLIENTS: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
_ARN_PREFIX = "arn:aws:secretsmanager:"


def _get_client(region: str) -> Any:
    """Return a process-wide boto3 Secrets Manager client for ``region``.

    botocore service-model parsing per client is ~tens of ms; reusing the
    client across the Lambda warm container makes per-request resolution free.
    """
    with _CACHE_LOCK:
        client = _CLIENTS.get(region)
        if client is None:
            client = boto3.client("secretsmanager", region_name=region)
            _CLIENTS[region] = client
        return client


def resolve_secret(env_var: str, default: str = "") -> str:
    """Return the plaintext value of ``env_var``, fetching from Secrets
    Manager when the env value is a secret ARN.
    """
    raw = os.environ.get(env_var, default)
    if not raw or not raw.startswith(_ARN_PREFIX):
        return raw

    with _CACHE_LOCK:
        cached = _SECRETS_CACHE.get(raw)
        if cached is not None:
            return cached

    region = raw.split(":")[3]
    response = _get_client(region).get_secret_value(SecretId=raw)
    value = response.get("SecretString", "")

    with _CACHE_LOCK:
        _SECRETS_CACHE[raw] = value
    return value
