"""Configuration management for secrets.json file."""

import json
import logging
from pathlib import Path
from typing import Any


class SecretsManager:
    """Manages loading and validation of secrets.json configuration file."""

    def __init__(self) -> None:
        """Initialize SecretsManager."""
        self.logger = logging.getLogger(__name__)

    def load_secrets(self, file_path: str) -> list[dict[str, str]]:
        """Load authentication information array from file.

        Args:
            file_path: Path to secrets.json file

        Returns:
            List of authentication configurations

        Raises:
            FileNotFoundError: If secrets file doesn't exist
            ValueError: If file format is invalid or validation fails
        """
        try:
            path = Path(file_path)
            if not path.exists():
                self.logger.error(f"Secrets file not found: {file_path}")
                raise FileNotFoundError(f"Secrets file not found: {file_path}")

            with open(path, encoding="utf-8") as f:
                secrets_data = json.load(f)

            if not isinstance(secrets_data, list):
                self.logger.error("Secrets file must contain an array")
                raise ValueError("Secrets file must contain an array")

            if not secrets_data:
                self.logger.error("Secrets array cannot be empty")
                raise ValueError("Secrets array cannot be empty")

            # Validate all configurations
            if not self.validate_secrets(secrets_data):
                raise ValueError("Invalid secrets configuration")

            self.logger.info(
                f"Successfully loaded {len(secrets_data)} authentication configurations"
            )
            return secrets_data

        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON format in secrets file: {str(e)}")
            raise ValueError(f"Invalid JSON format in secrets file: {str(e)}") from e

    def validate_secrets(self, secrets: list[dict[str, Any]]) -> bool:
        """Validate authentication information array integrity.

        Args:
            secrets: List of authentication configurations

        Returns:
            True if all configurations are valid, False otherwise
        """
        for i, config in enumerate(secrets):
            if not self.validate_single_config(config):
                self.logger.error(f"Invalid configuration at index {i}")
                return False
        return True

    def validate_single_config(self, config: dict[str, Any]) -> bool:
        """Validate single authentication configuration integrity.

        Args:
            config: Single authentication configuration

        Returns:
            True if configuration is valid, False otherwise
        """
        required_fields = ["clientId", "clientSecret", "tenant"]

        if not isinstance(config, dict):
            self.logger.error("Configuration must be a dictionary")
            return False

        for field in required_fields:
            if field not in config:
                self.logger.error(f"Missing required field: {field}")
                return False

            if not isinstance(config[field], str) or not config[field].strip():
                self.logger.error(f"Field {field} must be a non-empty string")
                return False

        return True
