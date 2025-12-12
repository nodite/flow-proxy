"""JWT token generator for Flow LLM Proxy authentication."""

import logging

import jwt


class JWTGenerator:
    """Generates JWT tokens required by Flow LLM Proxy."""

    def __init__(self) -> None:
        """Initialize JWT generator."""
        self.logger = logging.getLogger(__name__)

    def generate_token(self, config: dict[str, str]) -> str:
        """Generate JWT token based on authentication configuration.

        Args:
            config: Authentication configuration containing clientId, clientSecret,
                   tenant

        Returns:
            Generated JWT token string

        Raises:
            ValueError: If token generation fails
        """
        try:
            payload = self.create_jwt_payload(config)

            # Use clientSecret as the signing key for HS256
            secret_key = config["clientSecret"]

            # Generate JWT token with HS256 algorithm
            token: str = jwt.encode(payload, secret_key, algorithm="HS256")

            config_name = config.get("name", "unknown")
            self.logger.info(
                f"JWT token generated successfully for config: {config_name}"
            )

            return token

        except Exception as e:
            config_name = config.get("name", "unknown")
            self.logger.error(
                f"Failed to generate JWT token for config {config_name}: {str(e)}"
            )
            raise ValueError(f"JWT token generation failed: {str(e)}") from e

    def create_jwt_payload(self, config: dict[str, str]) -> dict[str, str]:
        """Create JWT payload from authentication configuration.

        Args:
            config: Authentication configuration

        Returns:
            JWT payload dictionary containing only clientId, clientSecret, tenant
        """
        payload = {
            "clientId": config["clientId"],
            "clientSecret": config["clientSecret"],
            "tenant": config["tenant"],
        }

        return payload

    def validate_token(self, token: str) -> bool:
        """Validate generated JWT token format.

        Args:
            token: JWT token to validate

        Returns:
            True if token format is valid, False otherwise
        """
        try:
            # Basic format validation - JWT should have 3 parts separated by dots
            parts = token.split(".")
            if len(parts) != 3:
                return False

            # Try to decode header without verification to check format
            jwt.get_unverified_header(token)

            return True

        except Exception:
            return False
