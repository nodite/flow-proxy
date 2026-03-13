"""Tests for shared state across plugin instances."""

from typing import Any

from flow_proxy_plugin.utils.process_services import ProcessServices


class TestSharedState:
    """Test suite for shared state functionality."""

    def test_shared_load_balancer_across_instances(
        self, sample_secrets_config: list[dict[str, str]], tmp_path: Any
    ) -> None:
        """Test that LoadBalancer state is shared via ProcessServices singleton."""
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
            ProcessServices.reset()

            # Access the singleton
            svc1 = ProcessServices.get()
            lb1 = svc1.load_balancer

            # Get first config
            config1 = lb1.get_next_config()
            assert config1["name"] == "config1"
            assert lb1.total_requests == 1

            # Access singleton again — must be the same instance
            svc2 = ProcessServices.get()
            lb2 = svc2.load_balancer

            assert svc2 is svc1, "ProcessServices must be the same singleton"
            assert lb2 is lb1, "LoadBalancer should be the same instance"

            # Get next config from same load balancer
            config2 = lb2.get_next_config()
            assert config2["name"] == "config2"
            assert lb2.total_requests == 2

            # Third access wraps around
            svc3 = ProcessServices.get()
            lb3 = svc3.load_balancer
            config3 = lb3.get_next_config()
            assert config3["name"] == "config1"
            assert lb3.total_requests == 3

        finally:
            # Restore original environment
            if original_env:
                os.environ["FLOW_PROXY_SECRETS_FILE"] = original_env
            elif "FLOW_PROXY_SECRETS_FILE" in os.environ:
                del os.environ["FLOW_PROXY_SECRETS_FILE"]

            ProcessServices.reset()

    def test_independent_jwt_generators(
        self, sample_secrets_config: list[dict[str, str]], tmp_path: Any
    ) -> None:
        """Test that JWTGenerator instance is shared via ProcessServices."""
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
            ProcessServices.reset()

            svc1 = ProcessServices.get()
            svc2 = ProcessServices.get()

            # Both references should point to the same singleton and same JWTGenerator
            assert svc1 is svc2, "ProcessServices singleton must be stable"
            assert svc1.jwt_generator is svc2.jwt_generator

        finally:
            if original_env:
                os.environ["FLOW_PROXY_SECRETS_FILE"] = original_env
            elif "FLOW_PROXY_SECRETS_FILE" in os.environ:
                del os.environ["FLOW_PROXY_SECRETS_FILE"]

            ProcessServices.reset()
