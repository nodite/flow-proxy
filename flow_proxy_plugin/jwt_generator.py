"""JWT token generator for Flow LLM Proxy authentication."""

import logging
from typing import Any

import jwt


class JWTGenerator:
    """Generates JWT tokens required for Flow LLM Proxy authentication."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Initialize JWT generator.

        Args:
            logger: Optional logger instance (creates new one if not provided)
        """
        self.logger = logger or logging.getLogger(__name__)
        self._algorithm = "HS256"

    def generate_token(self, config: dict[str, str]) -> str:
        """Generate JWT token based on authentication configuration.

        Args:
            config: Authentication configuration containing clientId, clientSecret, and tenant

        Returns:
            Generated JWT token string

        Raises:
            ValueError: If configuration is invalid or token generation fails
        """
        try:
            # Validate configuration has required fields
            self._validate_config(config)

            # Create JWT payload
            payload = self.create_jwt_payload(config)

            # Generate token using clientSecret as the signing key
            token = jwt.encode(
                payload, config["clientSecret"], algorithm=self._algorithm
            )

            # Validate the generated token
            if not self.validate_token(token, config["clientSecret"]):
                error_msg = "Generated token failed validation"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            config_name = config.get("name", config.get("clientId", "unknown"))
            self.logger.info(
                f"Successfully generated JWT token for configuration: '{config_name}'"
            )

            return token

        except KeyError as e:
            error_msg = f"Missing required field in configuration: {str(e)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
        except jwt.PyJWTError as e:
            error_msg = f"JWT encoding error: {str(e)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
        except Exception as e:
            error_msg = f"Unexpected error generating JWT token: {str(e)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e

    def create_jwt_payload(self, config: dict[str, str]) -> dict[str, Any]:
        """Create JWT payload from authentication configuration.

        Args:
            config: Authentication configuration

        Returns:
            JWT payload dictionary containing only clientId, clientSecret, and tenant
        """
        payload = {
            "clientId": config["clientId"],
            "clientSecret": config["clientSecret"],
            "tenant": config["tenant"],
        }

        return payload

    def validate_token(self, token: str, secret: str) -> bool:
        """Validate generated JWT token format and signature.

        Args:
            token: JWT token string to validate
            secret: Secret key used to sign the token

        Returns:
            True if token is valid, False otherwise
        """
        try:
            # Decode and verify the token
            decoded = jwt.decode(
                token,
                secret,
                algorithms=[self._algorithm],
                options={"verify_exp": False},
            )

            # Check that required fields are present in decoded payload
            required_fields = ["clientId", "clientSecret", "tenant"]
            for field in required_fields:
                if field not in decoded:
                    self.logger.error(f"Decoded token missing required field: {field}")
                    return False

            return True

        except jwt.InvalidTokenError as e:
            self.logger.error(f"Token validation failed: {str(e)}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during token validation: {str(e)}")
            return False

    def _validate_config(self, config: dict[str, str]) -> None:
        """Validate that configuration has all required fields.

        Args:
            config: Authentication configuration to validate

        Raises:
            ValueError: If configuration is missing required fields
        """
        required_fields = ["clientId", "clientSecret", "tenant"]
        missing_fields = [field for field in required_fields if field not in config]

        if missing_fields:
            error_msg = (
                f"Configuration missing required fields: {', '.join(missing_fields)}"
            )
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Check for empty values
        for field in required_fields:
            if not config[field] or not config[field].strip():
                error_msg = f"Configuration field '{field}' cannot be empty"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
