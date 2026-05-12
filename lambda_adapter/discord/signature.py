"""Discord request signature verification.

Implements Ed25519 signature verification for Discord Interactions API
webhooks, as required by Discord's security model.
"""

from __future__ import annotations


def verify_discord_signature(
    public_key: str,
    timestamp: str,
    body: str,
    signature: str,
) -> bool:
    """Verify a Discord interaction request signature.

    Discord signs requests using Ed25519.  The signed message is
    ``{timestamp}{body}`` and the signature is a hex-encoded 64-byte value
    provided in the ``X-Signature-Ed25519`` header.

    Args:
        public_key: The Discord application's public key (hex string).
        timestamp: Value of the ``X-Signature-Timestamp`` header.
        body: The raw request body string.
        signature: Value of the ``X-Signature-Ed25519`` header (hex string).

    Returns:
        ``True`` when the signature is valid; ``False`` otherwise.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
        key.verify(bytes.fromhex(signature), f"{timestamp}{body}".encode())
        return True
    except Exception:
        return False
