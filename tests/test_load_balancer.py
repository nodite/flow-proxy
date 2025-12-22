"""Tests for LoadBalancer class."""

import logging

import pytest

from flow_proxy_plugin.core.load_balancer import LoadBalancer


class TestLoadBalancer:
    """Test suite for LoadBalancer class."""

    def test_initialization_with_configs(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test LoadBalancer initialization with valid configs."""
        lb = LoadBalancer(sample_secrets_config)
        assert lb.total_count == 2
        assert lb.available_count == 2
        assert lb.failed_count == 0
        assert lb.total_requests == 0

    def test_initialization_with_empty_configs(self) -> None:
        """Test LoadBalancer initialization with empty configs raises ValueError."""
        with pytest.raises(
            ValueError, match="Cannot initialize LoadBalancer with empty configs list"
        ):
            LoadBalancer([])

    def test_round_robin_selection(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test round-robin selection cycles through configs."""
        lb = LoadBalancer(sample_secrets_config)

        # First request should get config1
        config1 = lb.get_next_config()
        assert config1["name"] == "config1"
        assert lb.total_requests == 1

        # Second request should get config2
        config2 = lb.get_next_config()
        assert config2["name"] == "config2"
        assert lb.total_requests == 2

        # Third request should wrap around to config1
        config3 = lb.get_next_config()
        assert config3["name"] == "config1"
        assert lb.total_requests == 3

    def test_mark_config_failed(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test marking a config as failed removes it from available list."""
        lb = LoadBalancer(sample_secrets_config)

        config = lb.get_next_config()
        assert config["name"] == "config1"

        # Mark first config as failed
        lb.mark_config_failed(config)
        assert lb.available_count == 1
        assert lb.failed_count == 1

        # Next request should skip to config2
        next_config = lb.get_next_config()
        assert next_config["name"] == "config2"

    def test_all_configs_failed(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test behavior when all configs are marked as failed."""
        lb = LoadBalancer(sample_secrets_config)

        # Mark all configs as failed
        for _ in range(2):
            config = lb.get_next_config()
            lb.mark_config_failed(config)

        assert lb.available_count == 0
        assert lb.failed_count == 2

        # Should raise RuntimeError when trying to get next config
        with pytest.raises(
            RuntimeError, match="No available authentication configurations"
        ):
            lb.get_next_config()

    def test_reset_failed_configs(
        self, sample_secrets_config: list[dict[str, str]]
    ) -> None:
        """Test resetting failed configs restores them to available list."""
        lb = LoadBalancer(sample_secrets_config)

        # Mark all configs as failed
        config1 = lb.get_next_config()
        lb.mark_config_failed(config1)
        config2 = lb.get_next_config()
        lb.mark_config_failed(config2)

        assert lb.available_count == 0
        assert lb.failed_count == 2

        # Reset failed configs
        lb.reset_failed_configs()
        assert lb.available_count == 2
        assert lb.failed_count == 0

        # Should be able to get configs again
        config = lb.get_next_config()
        assert config is not None

    def test_config_name_logging(
        self,
        sample_secrets_config: list[dict[str, str]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test that config names are logged correctly."""
        caplog.set_level(logging.INFO)
        lb = LoadBalancer(sample_secrets_config)

        lb.get_next_config()
        assert "Using authentication configuration: 'config1'" in caplog.text

    def test_mark_already_failed_config(
        self,
        sample_secrets_config: list[dict[str, str]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Test marking an already-failed config logs a warning."""
        caplog.set_level(logging.WARNING)
        lb = LoadBalancer(sample_secrets_config)

        config = lb.get_next_config()
        lb.mark_config_failed(config)
        lb.mark_config_failed(config)  # Try to mark again

        assert "Attempted to mark already-failed config as failed" in caplog.text

    def test_config_without_name_field(self) -> None:
        """Test handling configs without 'name' field."""
        configs = [
            {"clientId": "client-1", "clientSecret": "secret-1", "tenant": "tenant-1"},
            {"clientId": "client-2", "clientSecret": "secret-2", "tenant": "tenant-2"},
        ]
        lb = LoadBalancer(configs)

        config = lb.get_next_config()
        assert config["clientId"] == "client-1"
        assert lb.total_requests == 1
