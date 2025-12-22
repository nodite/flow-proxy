"""Tests for configuration management."""

import json
import tempfile
from typing import Any

import pytest

from flow_proxy_plugin.core.config import SecretsManager


class TestSecretsManager:
    """Test cases for SecretsManager class."""

    def test_load_valid_secrets(self, temp_secrets_file: str) -> None:
        """Test loading valid secrets configuration."""
        manager = SecretsManager()
        secrets = manager.load_secrets(temp_secrets_file)

        assert len(secrets) == 2
        assert secrets[0]["clientId"] == "client-id-1"
        assert secrets[1]["clientId"] == "client-id-2"

    def test_load_nonexistent_file(self) -> None:
        """Test loading from nonexistent file raises FileNotFoundError."""
        manager = SecretsManager()

        with pytest.raises(FileNotFoundError):
            manager.load_secrets("nonexistent.json")

    def test_load_invalid_json(self, invalid_secrets_file: str) -> None:
        """Test loading invalid JSON raises ValueError."""
        manager = SecretsManager()

        with pytest.raises(ValueError, match="Invalid JSON format"):
            manager.load_secrets(invalid_secrets_file)

    def test_load_empty_array(self) -> None:
        """Test loading empty array raises ValueError."""
        manager = SecretsManager()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump([], f)
            temp_file = f.name

        with pytest.raises(ValueError, match="Secrets array cannot be empty"):
            manager.load_secrets(temp_file)

    def test_validate_missing_required_fields(self) -> None:
        """Test validation fails for missing required fields."""
        manager = SecretsManager()

        # Missing clientId
        config: dict[str, Any] = {"clientSecret": "secret", "tenant": "tenant"}
        assert not manager.validate_single_config(config)

        # Missing clientSecret
        config = {"clientId": "client", "tenant": "tenant"}
        assert not manager.validate_single_config(config)

        # Missing tenant
        config = {"clientId": "client", "clientSecret": "secret"}
        assert not manager.validate_single_config(config)

    def test_validate_empty_fields(self) -> None:
        """Test validation fails for empty string fields."""
        manager = SecretsManager()

        config: dict[str, Any] = {
            "clientId": "",
            "clientSecret": "secret",
            "tenant": "tenant",
        }
        assert not manager.validate_single_config(config)

        config = {"clientId": "client", "clientSecret": "", "tenant": "tenant"}
        assert not manager.validate_single_config(config)

        config = {"clientId": "client", "clientSecret": "secret", "tenant": ""}
        assert not manager.validate_single_config(config)

    def test_validate_valid_config(self) -> None:
        """Test validation passes for valid configuration."""
        manager = SecretsManager()

        config: dict[str, Any] = {
            "clientId": "client-id",
            "clientSecret": "client-secret",
            "tenant": "tenant",
        }
        assert manager.validate_single_config(config)

    def test_validate_non_dict_config(self) -> None:
        """Test validation fails for non-dictionary configuration."""
        manager = SecretsManager()

        assert not manager.validate_single_config("not a dict")  # type: ignore[arg-type]
        assert not manager.validate_single_config(["list", "instead"])  # type: ignore[arg-type]
        assert not manager.validate_single_config(123)  # type: ignore[arg-type]

    def test_validate_non_string_fields(self) -> None:
        """Test validation fails for non-string field values."""
        manager = SecretsManager()

        config: dict[str, Any] = {
            "clientId": 123,
            "clientSecret": "secret",
            "tenant": "tenant",
        }
        assert not manager.validate_single_config(config)

        config = {"clientId": "client", "clientSecret": None, "tenant": "tenant"}
        assert not manager.validate_single_config(config)

    def test_load_not_array_json(self) -> None:
        """Test loading JSON that's not an array raises ValueError."""
        manager = SecretsManager()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"not": "an array"}, f)
            temp_file = f.name

        with pytest.raises(ValueError, match="Secrets file must contain an array"):
            manager.load_secrets(temp_file)
