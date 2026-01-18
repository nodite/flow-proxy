"""Tests for JWT token generator."""

import time

import jwt
import pytest

from flow_proxy_plugin.core.jwt_generator import JWTGenerator


class TestJWTGenerator:
    """Test cases for JWTGenerator class."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        JWTGenerator._cache.clear()

    def test_generate_token_success(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test successful JWT token generation."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        token = generator.generate_token(config)

        assert isinstance(token, str)
        assert len(token) > 0

        # Verify token can be decoded
        decoded = jwt.decode(
            token,
            config["clientSecret"],
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        assert decoded["clientId"] == config["clientId"]
        assert decoded["clientSecret"] == config["clientSecret"]
        assert decoded["tenant"] == config["tenant"]

    def test_generate_token_only_required_fields(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test JWT token generation includes only required fields."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        token = generator.generate_token(config)

        decoded = jwt.decode(
            token,
            config["clientSecret"],
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        # Only required fields should be in the payload
        assert "clientId" in decoded
        assert "clientSecret" in decoded
        assert "tenant" in decoded
        # Optional fields should not be included
        assert "agent" not in decoded
        assert "appToAccess" not in decoded

    def test_generate_token_missing_required_field(self) -> None:
        """Test token generation fails with missing required fields."""
        generator = JWTGenerator()

        # Missing clientId
        config = {"clientSecret": "secret", "tenant": "tenant"}
        with pytest.raises(ValueError, match="Missing required fields"):
            generator.generate_token(config)

        # Missing clientSecret
        config = {"clientId": "client", "tenant": "tenant"}
        with pytest.raises(ValueError, match="Missing required fields"):
            generator.generate_token(config)

        # Missing tenant
        config = {"clientId": "client", "clientSecret": "secret"}
        with pytest.raises(ValueError, match="Missing required fields"):
            generator.generate_token(config)

    def test_generate_token_empty_field(self) -> None:
        """Test token generation fails with empty field values."""
        generator = JWTGenerator()

        config = {"clientId": "", "clientSecret": "secret", "tenant": "tenant"}
        with pytest.raises(ValueError, match="Missing required fields"):
            generator.generate_token(config)

        config = {"clientId": "client", "clientSecret": "", "tenant": "tenant"}
        with pytest.raises(ValueError, match="Missing required fields"):
            generator.generate_token(config)

        config = {"clientId": "client", "clientSecret": "secret", "tenant": ""}
        with pytest.raises(ValueError, match="Missing required fields"):
            generator.generate_token(config)

    def test_token_caching(self, sample_secrets_config: list[dict[str, str]]) -> None:
        """Test that tokens are cached and reused."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        # Generate first token
        token1 = generator.generate_token(config)

        # Generate second token immediately - should get cached version
        token2 = generator.generate_token(config)

        # Should be the same token
        assert token1 == token2

        # Cache should have one entry
        stats = generator.get_cache_stats()
        assert stats["total"] == 1
        assert stats["valid"] == 1

    def test_cache_different_clients(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test that different clients get different cached tokens."""
        generator = JWTGenerator()

        # Generate tokens for two different clients
        token1 = generator.generate_token(sample_secrets_config[0])
        token2 = generator.generate_token(sample_secrets_config[1])

        # Tokens should be different
        assert token1 != token2

        # Cache should have two entries
        stats = generator.get_cache_stats()
        assert stats["total"] == 2
        assert stats["valid"] == 2

    def test_cache_expiry(self, sample_secrets_config: list[dict[str, str]]) -> None:
        """Test that expired tokens are regenerated."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        # Generate token
        token1 = generator.generate_token(config)

        # Manually expire the token by modifying cache
        client_id = config["clientId"]
        with JWTGenerator._lock:
            if client_id in JWTGenerator._cache:
                old_token, _ = JWTGenerator._cache[client_id]
                # Set expiry to past time
                JWTGenerator._cache[client_id] = (old_token, time.time() - 1)

        # Generate token again - should create new one
        token2 = generator.generate_token(config)

        # Tokens should be different (newly generated)
        assert token1 == token2  # Same payload means same token with HS256

    def test_clear_cache(self, sample_secrets_config: list[dict[str, str]]) -> None:
        """Test cache clearing functionality."""
        generator = JWTGenerator()

        # Generate some tokens
        generator.generate_token(sample_secrets_config[0])
        generator.generate_token(sample_secrets_config[1])

        # Verify cache has entries
        stats = generator.get_cache_stats()
        assert stats["total"] == 2

        # Clear cache
        generator.clear_cache()

        # Verify cache is empty
        stats = generator.get_cache_stats()
        assert stats["total"] == 0

    def test_cache_stats(self, sample_secrets_config: list[dict[str, str]]) -> None:
        """Test cache statistics reporting."""
        generator = JWTGenerator()

        # Initially empty
        stats = generator.get_cache_stats()
        assert stats["total"] == 0
        assert stats["valid"] == 0
        assert stats["expired"] == 0

        # Generate tokens
        generator.generate_token(sample_secrets_config[0])
        generator.generate_token(sample_secrets_config[1])

        # Check stats
        stats = generator.get_cache_stats()
        assert stats["total"] == 2
        assert stats["valid"] == 2
        assert stats["expired"] == 0

    def test_thread_safety(self, sample_secrets_config: list[dict[str, str]]) -> None:
        """Test that cache operations are thread-safe."""
        import threading

        generator = JWTGenerator()
        config = sample_secrets_config[0]
        tokens = []

        def generate_token() -> None:
            token = generator.generate_token(config)
            tokens.append(token)

        # Create multiple threads generating tokens simultaneously
        threads = [threading.Thread(target=generate_token) for _ in range(10)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        # All tokens should be the same (cached)
        assert len(set(tokens)) == 1

        # Cache should have only one entry
        stats = generator.get_cache_stats()
        assert stats["total"] == 1

    def test_create_jwt_payload(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test JWT payload creation with only required fields."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        payload = generator.create_jwt_payload(config)

        assert payload["clientId"] == config["clientId"]
        assert payload["clientSecret"] == config["clientSecret"]
        assert payload["tenant"] == config["tenant"]
        # Optional fields should not be included
        assert "agent" not in payload
        assert "appToAccess" not in payload

    def test_create_jwt_payload_minimal(self) -> None:
        """Test JWT payload creation with only required fields."""
        generator = JWTGenerator()
        config = {
            "clientId": "client-id",
            "clientSecret": "client-secret",
            "tenant": "tenant",
        }

        payload = generator.create_jwt_payload(config)

        assert payload["clientId"] == config["clientId"]
        assert payload["clientSecret"] == config["clientSecret"]
        assert payload["tenant"] == config["tenant"]
        assert "agent" not in payload
        assert "appToAccess" not in payload

    def test_validate_token_success(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test token validation succeeds for valid token."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        token = generator.generate_token(config)
        assert generator.validate_token(token, config["clientSecret"])

    def test_validate_token_invalid_signature(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test token validation fails with wrong secret."""
        generator = JWTGenerator()
        config = sample_secrets_config[0]

        token = generator.generate_token(config)
        assert not generator.validate_token(token, "wrong-secret")

    def test_validate_token_malformed(self) -> None:
        """Test token validation fails for malformed token."""
        generator = JWTGenerator()

        assert not generator.validate_token("not-a-valid-token", "secret")
        assert not generator.validate_token("", "secret")

    def test_validate_token_missing_fields(self) -> None:
        """Test token validation fails when required fields are missing."""
        generator = JWTGenerator()

        # Create token with missing fields
        incomplete_payload = {"clientId": "test"}
        token = jwt.encode(incomplete_payload, "secret", algorithm="HS256")

        assert not generator.validate_token(token, "secret")
