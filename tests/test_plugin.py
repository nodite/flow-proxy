"""Unit tests for FlowProxyPlugin main class."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from proxy.http.parser import HttpParser

from flow_proxy_plugin.plugin import FlowProxyPlugin


@pytest.fixture
def mock_secrets_file(tmp_path: Path) -> str:
    """Create a temporary secrets.json file for testing."""
    secrets_file = tmp_path / "secrets.json"
    secrets_content = """[
        {
            "name": "test-config-1",
            "clientId": "test-client-1",
            "clientSecret": "test-secret-1",
            "tenant": "test-tenant-1"
        },
        {
            "name": "test-config-2",
            "clientId": "test-client-2",
            "clientSecret": "test-secret-2",
            "tenant": "test-tenant-2"
        }
    ]"""
    secrets_file.write_text(secrets_content)
    return str(secrets_file)


@pytest.fixture
def mock_plugin_args() -> dict[str, Any]:
    """Create mock arguments for plugin initialization."""
    flags = Mock()
    flags.ca_cert_dir = None
    flags.ca_signing_key_file = None
    flags.ca_cert_file = None
    flags.ca_key_file = None

    client = Mock()
    event_queue = Mock()

    return {
        "uid": "test-uid",
        "flags": flags,
        "client": client,
        "event_queue": event_queue,
    }


class TestFlowProxyPluginInitialization:
    """Test FlowProxyPlugin initialization."""

    def test_plugin_initialization_success(
        self, mock_secrets_file: str, mock_plugin_args: dict[str, Any]
    ) -> None:
        """Test successful plugin initialization with valid configuration."""
        with patch("flow_proxy_plugin.plugin.SecretsManager.load_secrets") as mock_load:
            mock_load.return_value = [
                {
                    "name": "test-config-1",
                    "clientId": "test-client-1",
                    "clientSecret": "test-secret-1",
                    "tenant": "test-tenant-1",
                }
            ]

            plugin = FlowProxyPlugin(**mock_plugin_args)

            assert plugin is not None
            assert plugin.secrets_manager is not None
            assert plugin.load_balancer is not None
            assert plugin.jwt_generator is not None
            assert plugin.request_forwarder is not None
            assert len(plugin.configs) == 1

    def test_plugin_initialization_missing_secrets_file(
        self, mock_plugin_args: dict[str, Any]
    ) -> None:
        """Test plugin initialization fails when secrets.json is missing."""
        with patch("flow_proxy_plugin.plugin.SecretsManager.load_secrets") as mock_load:
            mock_load.side_effect = FileNotFoundError("Secrets file not found")

            with pytest.raises(FileNotFoundError):
                FlowProxyPlugin(**mock_plugin_args)

    def test_plugin_initialization_invalid_config(
        self, mock_plugin_args: dict[str, Any]
    ) -> None:
        """Test plugin initialization fails with invalid configuration."""
        with patch("flow_proxy_plugin.plugin.SecretsManager.load_secrets") as mock_load:
            mock_load.side_effect = ValueError("Invalid configuration")

            with pytest.raises(ValueError):
                FlowProxyPlugin(**mock_plugin_args)


class TestFlowProxyPluginRequestProcessing:
    """Test FlowProxyPlugin request processing."""

    @pytest.fixture
    def plugin(self, mock_plugin_args: dict[str, Any]) -> FlowProxyPlugin:
        """Create a plugin instance for testing."""
        with patch("flow_proxy_plugin.plugin.SecretsManager.load_secrets") as mock_load:
            mock_load.return_value = [
                {
                    "name": "test-config-1",
                    "clientId": "test-client-1",
                    "clientSecret": "test-secret-1",
                    "tenant": "test-tenant-1",
                },
                {
                    "name": "test-config-2",
                    "clientId": "test-client-2",
                    "clientSecret": "test-secret-2",
                    "tenant": "test-tenant-2",
                },
            ]

            return FlowProxyPlugin(**mock_plugin_args)

    def test_before_upstream_connection_success(self, plugin: FlowProxyPlugin) -> None:
        """Test successful request processing."""
        # Create mock request
        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"
        request.headers = {}

        # Mock validation, JWT generation, and header modification
        with patch.object(
            plugin.request_forwarder, "validate_request", return_value=True
        ), patch.object(
            plugin.jwt_generator, "generate_token", return_value="test-jwt-token"
        ) as mock_gen, patch.object(
            plugin.request_forwarder, "modify_request_headers", return_value=request
        ) as mock_modify:
            result = plugin.before_upstream_connection(request)

            assert result is not None
            assert mock_gen.called
            assert mock_modify.called

    def test_before_upstream_connection_invalid_request(
        self, plugin: FlowProxyPlugin
    ) -> None:
        """Test request processing with invalid request."""
        request = Mock(spec=HttpParser)

        # Mock validation to return False
        with patch.object(
            plugin.request_forwarder, "validate_request", return_value=False
        ):
            result = plugin.before_upstream_connection(request)
            assert result is None

    def test_before_upstream_connection_token_generation_failure(
        self, plugin: FlowProxyPlugin
    ) -> None:
        """Test request processing when token generation fails."""
        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"
        request.headers = {}

        # Mock validation and JWT generation failure
        with patch.object(
            plugin.request_forwarder, "validate_request", return_value=True
        ), patch.object(
            plugin.jwt_generator,
            "generate_token",
            side_effect=ValueError("Token generation failed"),
        ):
            result = plugin.before_upstream_connection(request)
            # Should return None when all configs fail
            assert result is None

    def test_before_upstream_connection_failover(self, plugin: FlowProxyPlugin) -> None:
        """Test failover to next config when token generation fails."""
        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"
        request.headers = {}

        # Mock validation, JWT generation with failover, and header modification
        with patch.object(
            plugin.request_forwarder, "validate_request", return_value=True
        ), patch.object(
            plugin.jwt_generator,
            "generate_token",
            side_effect=[ValueError("Token generation failed"), "test-jwt-token"],
        ) as mock_gen, patch.object(
            plugin.request_forwarder, "modify_request_headers", return_value=request
        ):
            result = plugin.before_upstream_connection(request)

            assert result is not None
            assert mock_gen.call_count == 2
            assert plugin.load_balancer.failed_count == 1

    def test_handle_client_request(self, plugin: FlowProxyPlugin) -> None:
        """Test handle_client_request delegates to before_upstream_connection."""
        request = Mock(spec=HttpParser)

        with patch.object(
            plugin, "before_upstream_connection", return_value=request
        ) as mock_before:
            result = plugin.handle_client_request(request)

            assert result == request
            mock_before.assert_called_once_with(request)


class TestFlowProxyPluginResponseProcessing:
    """Test FlowProxyPlugin response processing."""

    @pytest.fixture
    def plugin(self, mock_plugin_args: dict[str, Any]) -> FlowProxyPlugin:
        """Create a plugin instance for testing."""
        with patch("flow_proxy_plugin.plugin.SecretsManager.load_secrets") as mock_load:
            mock_load.return_value = [
                {
                    "name": "test-config-1",
                    "clientId": "test-client-1",
                    "clientSecret": "test-secret-1",
                    "tenant": "test-tenant-1",
                }
            ]

            return FlowProxyPlugin(**mock_plugin_args)

    def test_handle_upstream_chunk_success(self, plugin: FlowProxyPlugin) -> None:
        """Test successful upstream chunk handling."""
        chunk = memoryview(b"test response data")

        result = plugin.handle_upstream_chunk(chunk)

        assert result == chunk

    def test_handle_upstream_chunk_empty(self, plugin: FlowProxyPlugin) -> None:
        """Test handling empty upstream chunk."""
        chunk = memoryview(b"")

        result = plugin.handle_upstream_chunk(chunk)

        assert result == chunk

    def test_handle_upstream_chunk_error_handling(
        self, plugin: FlowProxyPlugin
    ) -> None:
        """Test error handling in upstream chunk processing."""
        chunk = memoryview(b"test data")

        # Mock request forwarder to raise exception
        with patch.object(
            plugin.request_forwarder,
            "handle_response_chunk",
            side_effect=Exception("Processing error"),
        ):
            # Should still return chunk to maintain connection stability
            result = plugin.handle_upstream_chunk(chunk)
            assert result == chunk

    def test_on_upstream_connection_close(self, plugin: FlowProxyPlugin) -> None:
        """Test upstream connection close handler."""
        # Should not raise any exceptions
        plugin.on_upstream_connection_close()

    def test_on_access_log(self, plugin: FlowProxyPlugin) -> None:
        """Test access log handler."""
        context = {"method": "GET", "path": "/v1/models"}

        result = plugin.on_access_log(context)

        assert result is not None
        assert "plugin" in result
        assert result["plugin"] == "FlowProxyPlugin"
        assert "load_balancer_stats" in result
        assert "available_configs" in result["load_balancer_stats"]
        assert "failed_configs" in result["load_balancer_stats"]
        assert "total_requests" in result["load_balancer_stats"]
