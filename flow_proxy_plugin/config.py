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
        self._validation_errors: list[str] = []

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
            # Resolve configuration file path
            resolved_path = self._resolve_config_path(file_path)

            if not resolved_path.exists():
                error_msg = f"Secrets file not found at resolved path: {resolved_path} (original: {file_path})"
                self.logger.error(error_msg)
                raise FileNotFoundError(error_msg)

            self.logger.info(f"Loading secrets from: {resolved_path}")

            with open(resolved_path, encoding="utf-8") as f:
                secrets_data = json.load(f)

            if not isinstance(secrets_data, list):
                error_msg = f"Secrets file must contain an array, got {type(secrets_data).__name__}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            if not secrets_data:
                error_msg = "Secrets array cannot be empty - at least one authentication configuration is required"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            # Validate all configurations
            if not self.validate_secrets(secrets_data):
                error_msg = "One or more authentication configurations are invalid"
                self.logger.error(error_msg)
                raise ValueError(error_msg)

            self.logger.info(
                f"Successfully loaded and validated {len(secrets_data)} authentication configurations from {resolved_path}"
            )
            return secrets_data

        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format in secrets file {resolved_path if 'resolved_path' in locals() else file_path}: {str(e)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e
        except Exception as e:
            if isinstance(e, FileNotFoundError | ValueError):
                raise
            error_msg = f"Unexpected error loading secrets file {file_path}: {str(e)}"
            self.logger.error(error_msg)
            raise ValueError(error_msg) from e

    def validate_secrets(self, secrets: list[dict[str, Any]]) -> bool:
        """Validate authentication information array integrity.

        Args:
            secrets: List of authentication configurations

        Returns:
            True if all configurations are valid, False otherwise
        """
        self._validation_errors.clear()
        is_valid = True

        for i, config in enumerate(secrets):
            if not self.validate_single_config(config):
                error_msg = f"Invalid configuration at index {i}: {', '.join(self._validation_errors)}"
                self.logger.error(error_msg)
                is_valid = False
                self._validation_errors.clear()  # Clear for next config

        return is_valid

    def validate_single_config(self, config: dict[str, Any]) -> bool:
        """Validate single authentication configuration integrity.

        Args:
            config: Single authentication configuration

        Returns:
            True if configuration is valid, False otherwise
        """
        required_fields = ["clientId", "clientSecret", "tenant"]
        self._validation_errors.clear()

        if not isinstance(config, dict):
            error_msg = (
                f"Configuration must be a dictionary, got {type(config).__name__}"
            )
            self._validation_errors.append(error_msg)
            return False

        # Check for missing required fields
        missing_fields = [field for field in required_fields if field not in config]
        if missing_fields:
            error_msg = f"Missing required fields: {', '.join(missing_fields)}"
            self._validation_errors.append(error_msg)

        # Check for empty or invalid field values
        for field in required_fields:
            if field in config:
                value = config[field]
                if not isinstance(value, str):
                    error_msg = (
                        f"Field '{field}' must be a string, got {type(value).__name__}"
                    )
                    self._validation_errors.append(error_msg)
                elif not value.strip():
                    error_msg = f"Field '{field}' cannot be empty or whitespace-only"
                    self._validation_errors.append(error_msg)

        # Log detailed validation errors
        if self._validation_errors:
            for error in self._validation_errors:
                self.logger.error(error)
            return False

        return True

    def _resolve_config_path(self, file_path: str) -> Path:
        """Resolve configuration file path with support for relative and absolute paths.

        Args:
            file_path: Original file path (can be relative or absolute)

        Returns:
            Resolved Path object
        """
        path = Path(file_path)

        # If it's already absolute, return as-is
        if path.is_absolute():
            return path

        # Try relative to current working directory first
        if path.exists():
            return path.resolve()

        # Try relative to the package directory
        package_dir = Path(__file__).parent.parent
        package_relative = package_dir / path
        if package_relative.exists():
            return package_relative.resolve()

        # Return the original path for proper error handling
        return path.resolve()
