"""Pytest configuration and fixtures."""

import json
import tempfile

import pytest


@pytest.fixture
def sample_secrets_config() -> list[dict[str, str]]:
    """Sample secrets configuration for testing."""
    return [
        {
            "name": "config1",
            "agent": "simple_agent",
            "appToAccess": "llm-api",
            "clientId": "client-id-1",
            "clientSecret": "client-secret-1",
            "tenant": "tenant1",
        },
        {
            "name": "config2",
            "agent": "simple_agent",
            "appToAccess": "llm-api",
            "clientId": "client-id-2",
            "clientSecret": "client-secret-2",
            "tenant": "tenant2",
        },
    ]


@pytest.fixture
def temp_secrets_file(sample_secrets_config: list[dict[str, str]]) -> str:
    """Create temporary secrets.json file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(sample_secrets_config, f)
        return f.name


@pytest.fixture
def invalid_secrets_file() -> str:
    """Create temporary invalid secrets.json file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("invalid json content")
        return f.name
