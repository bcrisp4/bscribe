"""Token minting and hashing.

One definition site for the secret format and hash algorithm: the CLI mints
secrets and the server hashes presented bearer tokens, and both must agree.
Stores never see plaintext secrets — only :class:`~bscribe.domain.models.Token`
records carrying the hash.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from bscribe.domain.models import Token

SECRET_PREFIX = "bscribe_"  # noqa: S105 - marker prefix, not a credential
"""Marker prefix on generated secrets — makes leaked secrets grep-able and
secret-scanner friendly. The full string, prefix included, is what gets
hashed; nothing ever parses it."""


def generate_secret() -> str:
    """Generate a new bearer-token secret (256 bits, URL-safe, prefixed).

    Returns:
        The plaintext secret. Callers must show it once and discard it.
    """
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    """Hash a secret exactly as it is stored and looked up.

    Unsalted SHA-256 is deliberate: secrets are 256-bit random keys, not
    passwords — there is no dictionary to attack (docs/adr/0002).

    Args:
        secret: The full presented secret string, prefix included.

    Returns:
        64-char lowercase hex digest.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def mint_token(label: str) -> tuple[Token, str]:
    """Create a new token principal.

    Args:
        label: Human-readable caller name; mutable in spirit, but the
            returned token id is the immutable identity.

    Returns:
        The token record (hash only) and the plaintext secret, which is
        shown to the operator once and is otherwise unrecoverable.
    """
    secret = generate_secret()
    token = Token(
        id=secrets.token_hex(4),
        label=label,
        secret_hash=hash_secret(secret),
        created_at=datetime.now(tz=UTC),
    )
    return token, secret
