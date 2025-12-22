"""Tests for JWT token generator."""

import jwt
import pytest

from flow_proxy_plugin.core.jwt_generator import JWTGenerator


class TestJWTGenerator:
    """Test cases for JWTGenerator class."""

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
        with pytest.raises(ValueError, match="missing required fields"):
            generator.generate_token(config)

        # Missing clientSecret
        config = {"clientId": "client", "tenant": "tenant"}
        with pytest.raises(ValueError, match="missing required fields"):
            generator.generate_token(config)

        # Missing tenant
        config = {"clientId": "client", "clientSecret": "secret"}
        with pytest.raises(ValueError, match="missing required fields"):
            generator.generate_token(config)

    def test_generate_token_empty_field(self) -> None:
        """Test token generation fails with empty field values."""
        generator = JWTGenerator()

        config = {"clientId": "", "clientSecret": "secret", "tenant": "tenant"}
        with pytest.raises(ValueError, match="cannot be empty"):
            generator.generate_token(config)

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
