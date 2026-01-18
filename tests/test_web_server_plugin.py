"""Unit tests for FlowProxyWebServerPlugin."""

from typing import Any
from unittest.mock import Mock, patch

import pytest
import requests
from proxy.http.parser import HttpParser

from flow_proxy_plugin.plugins.web_server_plugin import FlowProxyWebServerPlugin


@pytest.fixture
def mock_plugin_args() -> dict[str, Any]:
    """Create mock arguments for plugin initialization."""
    flags = Mock()
    flags.ca_cert_dir = None
    flags.ca_signing_key_file = None
    flags.ca_cert_file = None
    flags.ca_key_file = None
    flags.log_level = "INFO"

    client = Mock()
    client.connection = Mock()  # Mock connection for _is_client_connected
    event_queue = Mock()

    return {
        "uid": "test-uid",
        "flags": flags,
        "client": client,
        "event_queue": event_queue,
    }


@pytest.fixture
def plugin(mock_plugin_args: dict[str, Any]) -> FlowProxyWebServerPlugin:
    """Create a plugin instance for testing."""
    # Clear shared state before test
    from flow_proxy_plugin.utils.plugin_base import SharedComponentManager

    SharedComponentManager().reset()

    with patch(
        "flow_proxy_plugin.core.config.SecretsManager.load_secrets"
    ) as mock_load:
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

        return FlowProxyWebServerPlugin(**mock_plugin_args)


class TestFlowProxyWebServerPluginInitialization:
    """Test FlowProxyWebServerPlugin initialization."""

    def test_plugin_initialization_success(
        self, mock_plugin_args: dict[str, Any]
    ) -> None:
        """Test successful plugin initialization."""
        # Clear shared state before test
        from flow_proxy_plugin.utils.plugin_base import SharedComponentManager

        SharedComponentManager().reset()

        with patch(
            "flow_proxy_plugin.core.config.SecretsManager.load_secrets"
        ) as mock_load:
            mock_load.return_value = [
                {
                    "name": "test-config",
                    "clientId": "test-client",
                    "clientSecret": "test-secret",
                    "tenant": "test-tenant",
                }
            ]

            plugin = FlowProxyWebServerPlugin(**mock_plugin_args)

            assert plugin is not None
            assert plugin.secrets_manager is not None
            assert plugin.load_balancer is not None
            assert plugin.jwt_generator is not None
            assert plugin.request_forwarder is not None
            assert len(plugin.configs) == 1

    def test_plugin_initialization_failure(
        self, mock_plugin_args: dict[str, Any]
    ) -> None:
        """Test plugin initialization failure."""
        # Clear shared state before test
        from flow_proxy_plugin.utils.plugin_base import SharedComponentManager

        SharedComponentManager().reset()

        with patch(
            "flow_proxy_plugin.core.config.SecretsManager.load_secrets"
        ) as mock_load:
            mock_load.side_effect = FileNotFoundError("Secrets file not found")

            with pytest.raises(FileNotFoundError):
                FlowProxyWebServerPlugin(**mock_plugin_args)

    def test_routes(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test routes configuration."""
        routes = plugin.routes()
        assert len(routes) == 1
        assert routes[0][1] == r"/.*"


class TestConnectionStateCheck:
    """Test connection state checking."""

    def test_is_client_connected_true(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test client connection check returns True when connected."""
        mock_connection = Mock()
        plugin.client = Mock(connection=mock_connection)
        assert plugin._is_client_connected() is True

    def test_is_client_connected_false_no_connection(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test client connection check returns False when no connection."""
        plugin.client = Mock(connection=None)
        assert plugin._is_client_connected() is False

    def test_is_client_connected_false_no_attribute(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test client connection check returns False when attribute missing."""
        delattr(plugin.client, "connection")
        assert plugin._is_client_connected() is False

    def test_is_client_connected_exception_handling(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test client connection check handles exceptions."""
        # Create a Mock that raises exception when accessing connection
        mock_client = Mock()
        type(mock_client).connection = property(
            lambda self: (_ for _ in ()).throw(Exception("Test error"))
        )
        plugin.client = mock_client
        assert plugin._is_client_connected() is False


class TestPrepareHeaders:
    """Test header preparation."""

    def test_prepare_headers_basic(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test basic header preparation."""
        request = Mock(spec=HttpParser)
        request.headers = {}

        headers = plugin._prepare_headers(request, "test-jwt-token")

        assert headers["Authorization"] == "Bearer test-jwt-token"
        assert headers["Host"] == "flow.ciandt.com"

    def test_prepare_headers_with_existing_headers(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test header preparation with existing headers."""
        request = Mock(spec=HttpParser)
        request.headers = {
            b"content-type": (b"application/json", b""),
            b"user-agent": (b"test-agent", b""),
            b"host": (b"old-host", b""),  # Should be skipped
            b"authorization": (b"old-auth", b""),  # Should be skipped
        }

        headers = plugin._prepare_headers(request, "test-jwt-token")

        assert headers["Authorization"] == "Bearer test-jwt-token"
        assert headers["Host"] == "flow.ciandt.com"
        assert headers["content-type"] == "application/json"
        assert headers["user-agent"] == "test-agent"


class TestSendResponse:
    """Test response sending with improved error handling."""

    def test_send_response_success(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test successful response sending."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]

        mock_queue = Mock()
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush)

        plugin._send_response(mock_response)

        assert mock_queue.call_count >= 4  # Status, headers, separator, chunks

    def test_send_response_broken_pipe_during_streaming(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test response sending handles BrokenPipeError gracefully."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "text/event-stream"}
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2", b"chunk3"]

        mock_queue = Mock(side_effect=[None, None, None, BrokenPipeError()])
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush)

        # Should not raise exception
        plugin._send_response(mock_response)

    def test_send_response_connection_reset(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test response sending handles ConnectionResetError gracefully."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]

        mock_queue = Mock(side_effect=[None, None, None, ConnectionResetError()])
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush)

        # Should not raise exception
        plugin._send_response(mock_response)

    def test_send_response_os_error_broken_pipe(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test response sending handles OSError with errno 32 (Broken pipe)."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2"]

        os_error = OSError("Broken pipe")
        os_error.errno = 32
        mock_queue = Mock(side_effect=[None, None, None, os_error])
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush)

        # Should not raise exception
        plugin._send_response(mock_response)

    def test_send_response_client_disconnected(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test response sending stops when client disconnects."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b"chunk1", b"chunk2", b"chunk3"]

        mock_queue = Mock()
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush, connection=None)

        plugin._send_response(mock_response)

    def test_send_response_unexpected_error(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test response sending handles unexpected errors."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b"chunk1"]

        mock_queue = Mock(side_effect=[None, None, None, RuntimeError("Unexpected")])
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush)

        # Should not raise exception
        plugin._send_response(mock_response)

    def test_send_response_empty_chunks(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test response sending skips empty chunks."""
        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b"chunk1", b"", b"chunk2"]

        mock_queue = Mock()
        mock_flush = Mock()
        plugin.client = Mock(queue=mock_queue, flush=mock_flush)

        plugin._send_response(mock_response)


class TestSendError:
    """Test error response sending."""

    def test_send_error_default(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test sending default error response."""
        mock_queue = Mock()
        plugin.client = Mock(queue=mock_queue)

        plugin._send_error()

        mock_queue.assert_called_once()
        call_args = mock_queue.call_args[0][0]
        response_bytes = bytes(call_args)
        assert b"500 Error" in response_bytes
        assert b"Internal server error" in response_bytes

    def test_send_error_custom(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test sending custom error response."""
        mock_queue = Mock()
        plugin.client = Mock(queue=mock_queue)

        plugin._send_error(status_code=404, message="Not found")

        mock_queue.assert_called_once()
        call_args = mock_queue.call_args[0][0]
        response_bytes = bytes(call_args)
        assert b"404 Error" in response_bytes
        assert b"Not found" in response_bytes


class TestHandleRequest:
    """Test request handling."""

    def test_handle_request_success(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test successful request handling."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b'{"model": "gpt-3.5-turbo"}'
        request.headers = {b"content-type": (b"application/json", b"")}

        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.iter_content.return_value = [b'{"response": "ok"}']
        mock_response.close = Mock()

        with patch("requests.request", return_value=mock_response):
            plugin.handle_request(request)

        mock_response.close.assert_called_once()

    def test_handle_request_with_buffer(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test request handling with buffer attribute."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.buffer = bytearray(b'{"model": "gpt-3.5-turbo"}')
        request.headers = {}  # Add headers attribute
        delattr(request, "body")  # Simulate missing body attribute

        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.iter_content.return_value = []
        mock_response.close = Mock()

        with patch("requests.request", return_value=mock_response):
            plugin.handle_request(request)

        mock_response.close.assert_called_once()

    def test_handle_request_failure(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Test request handling with failure."""
        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"

        with (
            patch("requests.request", side_effect=Exception("Connection failed")),
            patch.object(plugin, "_send_error") as mock_error,
        ):
            plugin.handle_request(request)

            mock_error.assert_called_once()

    def test_handle_request_with_timeout_tuple(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Test request uses timeout tuple for streaming."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b'{"model": "gpt-3.5-turbo"}'
        request.headers = {}

        mock_response = Mock(spec=requests.Response)
        mock_response.status_code = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.iter_content.return_value = []
        mock_response.close = Mock()

        with patch("requests.request", return_value=mock_response) as mock_req:
            plugin.handle_request(request)

            # Verify timeout tuple is used (connect_timeout, read_timeout)
            assert mock_req.called
            call_kwargs = mock_req.call_args[1]
            assert "timeout" in call_kwargs
            assert call_kwargs["timeout"] == (30, 600)
