"""Tests for bscribe.domain.tokens."""

from __future__ import annotations

import hashlib
from datetime import UTC

from bscribe.domain.tokens import (
    SECRET_PREFIX,
    generate_secret,
    hash_secret,
    mint_token,
)


class TestGenerateSecret:
    def test_secret_carries_bscribe_prefix(self) -> None:
        assert generate_secret().startswith(SECRET_PREFIX)

    def test_secret_has_substantial_entropy_suffix(self) -> None:
        # token_urlsafe(32) encodes 32 random bytes as ~43 urlsafe chars.
        suffix = generate_secret().removeprefix(SECRET_PREFIX)
        assert len(suffix) >= 40

    def test_secrets_are_unique_across_calls(self) -> None:
        secrets_batch = {generate_secret() for _ in range(50)}
        assert len(secrets_batch) == 50


class TestHashSecret:
    def test_hash_is_sha256_hexdigest_of_full_string(self) -> None:
        secret = "bscribe_example"
        expected = hashlib.sha256(secret.encode()).hexdigest()
        assert hash_secret(secret) == expected

    def test_hash_is_deterministic(self) -> None:
        secret = generate_secret()
        assert hash_secret(secret) == hash_secret(secret)


class TestMintToken:
    def test_token_hash_matches_returned_secret(self) -> None:
        token, secret = mint_token("bsearch")
        assert token.secret_hash == hash_secret(secret)

    def test_token_id_is_eight_hex_chars(self) -> None:
        token, _ = mint_token("bsearch")
        assert len(token.id) == 8
        int(token.id, 16)  # raises ValueError if not hex

    def test_token_label_is_preserved(self) -> None:
        token, _ = mint_token("adhoc")
        assert token.label == "adhoc"

    def test_created_at_is_utc_aware(self) -> None:
        token, _ = mint_token("bsearch")
        assert token.created_at.tzinfo is UTC

    def test_minted_ids_are_unique(self) -> None:
        ids = {mint_token("x")[0].id for _ in range(50)}
        assert len(ids) == 50
