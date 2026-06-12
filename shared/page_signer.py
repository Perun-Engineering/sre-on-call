"""Sign the #33 incident-page CloudFront URL.

The master signs the *stable* page URL (``/pages/<investigation_id>.html``) at
report-post time. Signing needs no existing object — it is an RSA signature over
a CloudFront canned policy — so the link can ship in the initial report even
though the page renders moments later at finalize.

Fail-open and gated: :meth:`from_env` returns ``None`` unless
``INCIDENT_PAGE_ENABLED`` is set and all required vars are present; :meth:`sign`
returns ``None`` on any error, so the report simply omits the link.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 7 * 86400  # 7 days


class CloudFrontUrlSigner:
    """Builds + signs the stable per-investigation page URL."""

    def __init__(
        self,
        *,
        base_url: str,
        key_pair_id: str,
        private_key_pem: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_pair_id = key_pair_id
        self._private_key_pem = private_key_pem
        self._ttl_seconds = ttl_seconds

    @classmethod
    def from_env(cls, secrets_client: Any = None) -> "CloudFrontUrlSigner | None":
        """Build from env, fetching the private key from Secrets Manager.

        Returns ``None`` when disabled or any required var is missing.
        """
        if os.environ.get("INCIDENT_PAGE_ENABLED", "").strip().lower() != "true":
            return None
        base_url = os.environ.get("INCIDENT_PAGE_BASE_URL", "").strip()
        key_pair_id = os.environ.get("CLOUDFRONT_KEY_PAIR_ID", "").strip()
        secret_arn = os.environ.get("CLOUDFRONT_PRIVATE_KEY_SECRET_ARN", "").strip()
        if not base_url or not key_pair_id or not secret_arn:
            return None
        try:
            ttl = int(os.environ.get("INCIDENT_PAGE_URL_TTL_SECONDS", _DEFAULT_TTL_SECONDS))
        except ValueError:
            ttl = _DEFAULT_TTL_SECONDS
        try:
            if secrets_client is None:
                import boto3

                secrets_client = boto3.client(
                    "secretsmanager",
                    region_name=os.environ.get("AWS_REGION", "us-east-1"),
                )
            pem = secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]
        except Exception:
            logger.exception("CloudFrontUrlSigner.from_env: failed to load private key")
            return None
        return cls(
            base_url=base_url,
            key_pair_id=key_pair_id,
            private_key_pem=pem,
            ttl_seconds=ttl,
        )

    def sign(self, investigation_id: str) -> str | None:
        """Return a signed URL for the investigation's page, or ``None`` on error."""
        try:
            from botocore.signers import CloudFrontSigner
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

            private_key = serialization.load_pem_private_key(
                self._private_key_pem.encode("utf-8"), password=None
            )
            if not isinstance(private_key, RSAPrivateKey):
                raise TypeError(f"Expected RSA private key, got {type(private_key)}")

            def _rsa_signer(message: bytes) -> bytes:
                return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303

            url = f"{self._base_url}/pages/{investigation_id}.html"
            expires = datetime.now(tz=timezone.utc) + timedelta(seconds=self._ttl_seconds)
            signer = CloudFrontSigner(self._key_pair_id, _rsa_signer)
            return signer.generate_presigned_url(url, date_less_than=expires)
        except Exception:
            logger.exception(
                "CloudFrontUrlSigner.sign failed (investigation_id=%s)", investigation_id
            )
            return None
