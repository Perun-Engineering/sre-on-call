"""Unit tests for shared.page_signer — CloudFront signed page URLs."""
from __future__ import annotations

from unittest.mock import MagicMock

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

from shared.page_signer import CloudFrontUrlSigner


def _private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _signer(pem: str | None = None) -> CloudFrontUrlSigner:
    return CloudFrontUrlSigner(
        base_url="https://d111.cloudfront.net",
        key_pair_id="K123",
        private_key_pem=pem if pem is not None else _private_pem(),
        ttl_seconds=3600,
    )


def test_sign_returns_signed_url_for_stable_page_key():
    url = _signer().sign("inv-7")
    assert url is not None
    assert url.startswith("https://d111.cloudfront.net/pages/inv-7.html?")
    assert "Key-Pair-Id=K123" in url
    assert "Signature=" in url
    assert "Expires=" in url


def test_sign_is_fail_open_on_bad_key():
    assert _signer(pem="not a pem").sign("inv-7") is None


def test_from_env_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("INCIDENT_PAGE_ENABLED", raising=False)
    assert CloudFrontUrlSigner.from_env() is None


def test_from_env_returns_none_when_enabled_but_unconfigured(monkeypatch):
    monkeypatch.setenv("INCIDENT_PAGE_ENABLED", "true")
    monkeypatch.delenv("INCIDENT_PAGE_BASE_URL", raising=False)
    assert CloudFrontUrlSigner.from_env() is None


def test_from_env_builds_signer_and_loads_key_from_secrets(monkeypatch):
    monkeypatch.setenv("INCIDENT_PAGE_ENABLED", "true")
    monkeypatch.setenv("INCIDENT_PAGE_BASE_URL", "https://d111.cloudfront.net")
    monkeypatch.setenv("CLOUDFRONT_KEY_PAIR_ID", "K123")
    monkeypatch.setenv("CLOUDFRONT_PRIVATE_KEY_SECRET_ARN", "arn:secret")
    fake_sm = MagicMock()
    fake_sm.get_secret_value.return_value = {"SecretString": _private_pem()}
    signer = CloudFrontUrlSigner.from_env(secrets_client=fake_sm)
    assert signer is not None
    signed_url = signer.sign("inv-1")
    assert signed_url is not None
    assert signed_url.startswith("https://d111.cloudfront.net/pages/inv-1.html?")
