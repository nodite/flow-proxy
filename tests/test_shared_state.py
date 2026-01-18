"""Tests for shared state across plugin instances."""

import logging
from typing import Any

from flow_proxy_plugin.utils.plugin_base import (
    SharedComponentManager,
    initialize_plugin_components,
)


class TestSharedState:
    """Test suite for shared state functionality."""

    def test_shared_load_balancer_across_instances(
        self, sample_secrets_config: list[dict[str, str]], tmp_path: Any
    ) -> None:
        """Test that LoadBalancer state is shared across multiple plugin instances."""
        import json
        import os

        # Create a temporary secrets.json file
        secrets_file = tmp_path / "secrets.json"
        with open(secrets_file, "w") as f:
            json.dump(sample_secrets_config, f)

        # Set environment variable for secrets file path
        original_env = os.environ.get("FLOW_PROXY_SECRETS_FILE")
        os.environ["FLOW_PROXY_SECRETS_FILE"] = str(secrets_file)

        try:
            # Clear shared state first
            manager = SharedComponentManager()
            manager.reset()

            logger = logging.getLogger(__name__)

            # Initialize first instance
            _, _, lb1, _, _ = initialize_plugin_components(logger)

            # Get first config from first instance
            config1 = lb1.get_next_config()
            assert config1["name"] == "config1"
            assert lb1.total_requests == 1

            # Initialize second instance - should reuse the same LoadBalancer
            _, _, lb2, _, _ = initialize_plugin_components(logger)

            # Verify it's the same instance
            assert lb2 is lb1, "LoadBalancer should be the same instance"

            # Get next config from second instance
            config2 = lb2.get_next_config()
            # Should get config2 since lb1 already got config1
            assert config2["name"] == "config2"
            assert lb2.total_requests == 2

            # Initialize third instance
            _, _, lb3, _, _ = initialize_plugin_components(logger)

            # Get next config from third instance
            config3 = lb3.get_next_config()
            # Should wrap around to config1
            assert config3["name"] == "config1"
            assert lb3.total_requests == 3

        finally:
            # Restore original environment
            if original_env:
                os.environ["FLOW_PROXY_SECRETS_FILE"] = original_env
            elif "FLOW_PROXY_SECRETS_FILE" in os.environ:
                del os.environ["FLOW_PROXY_SECRETS_FILE"]

            # Clean up shared state
            manager = SharedComponentManager()
            manager.reset()

    def test_independent_jwt_generators(
        self, sample_secrets_config: list[dict[str, str]], tmp_path: Any
    ) -> None:
        """Test that JWTGenerator instances are independent."""
        import json
        import os

        # Create a temporary secrets.json file
        secrets_file = tmp_path / "secrets.json"
        with open(secrets_file, "w") as f:
            json.dump(sample_secrets_config, f)

        original_env = os.environ.get("FLOW_PROXY_SECRETS_FILE")
        os.environ["FLOW_PROXY_SECRETS_FILE"] = str(secrets_file)

        try:
            # Clear shared state
            manager = SharedComponentManager()
            manager.reset()

            logger = logging.getLogger(__name__)

            # Initialize two instances
            _, _, _, jwt1, _ = initialize_plugin_components(logger)
            _, _, _, jwt2, _ = initialize_plugin_components(logger)

            # JWT generators should be different instances
            assert jwt1 is not jwt2, "JWTGenerator instances should be independent"

        finally:
            if original_env:
                os.environ["FLOW_PROXY_SECRETS_FILE"] = original_env
            elif "FLOW_PROXY_SECRETS_FILE" in os.environ:
                del os.environ["FLOW_PROXY_SECRETS_FILE"]

            # Clean up
            manager = SharedComponentManager()
            manager.reset()
