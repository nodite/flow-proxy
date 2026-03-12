"""Unit tests for FlowProxyWebServerPlugin."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest
from proxy.http.parser import HttpParser

import flow_proxy_plugin.plugins.web_server_plugin as ws_mod
from flow_proxy_plugin.plugins.web_server_plugin import (
    FlowProxyWebServerPlugin,
    StreamStats,
)
from flow_proxy_plugin.utils.process_services import ProcessServices


class TestStreamStats:
    """Unit tests for StreamStats dataclass."""

    def test_ttft_ms_none_when_no_first_chunk(self) -> None:
        stats = StreamStats(start_time=1000.0)
        assert stats.ttft_ms is None

    def test_ttft_ms_calculated_correctly(self) -> None:
        stats = StreamStats(start_time=1000.0, first_chunk_time=1000.042)
        assert abs(stats.ttft_ms - 42.0) < 0.001  # type: ignore[operator]

    def test_duration_ms_none_when_no_first_chunk(self) -> None:
        stats = StreamStats(start_time=1000.0, end_time=1001.0)
        assert stats.duration_ms is None

    def test_duration_ms_none_when_no_end_time(self) -> None:
        stats = StreamStats(start_time=1000.0, first_chunk_time=1000.042)
        assert stats.duration_ms is None

    def test_duration_ms_calculated_correctly(self) -> None:
        stats = StreamStats(
            start_time=1000.0,
            first_chunk_time=1000.042,
            end_time=1003.292,
        )
        assert abs(stats.duration_ms - 3250.0) < 0.001  # type: ignore[operator]

    def test_defaults(self) -> None:
        stats = StreamStats(start_time=0.0)
        assert stats.bytes_sent == 0
        assert stats.chunks_sent == 0
        assert stats.event_count == 0
        assert stats.completed is False


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
def mock_svc() -> MagicMock:
    """Shared ProcessServices mock reused across fixtures and tests."""
    svc = MagicMock()
    svc.configs = [
        {
            "name": "test-config-1",
            "clientId": "test-client-1",
            "clientSecret": "test-secret-1",
            "tenant": "test-tenant-1",
        },
    ]
    svc.load_balancer = MagicMock()
    svc.jwt_generator = MagicMock()
    svc.request_forwarder = MagicMock()
    svc.request_forwarder.target_base_url = "https://flow.ciandt.com"
    svc.request_forwarder.validate_request.return_value = True
    svc.request_filter = MagicMock()
    svc.http_client = MagicMock()
    svc.get_http_client.return_value = svc.http_client
    return svc


@pytest.fixture(autouse=True)
def reset_state(mock_svc: MagicMock) -> Generator[None, None, None]:
    """Reset ProcessServices singleton and pool module vars before/after each test."""
    ProcessServices.reset()
    ws_mod._web_pool = None
    yield
    ProcessServices.reset()
    ws_mod._web_pool = None


@pytest.fixture
def plugin(
    mock_plugin_args: dict[str, Any], mock_svc: MagicMock
) -> Generator[FlowProxyWebServerPlugin, None, None]:
    """Create a plugin instance for testing."""
    with patch.object(ProcessServices, "get", return_value=mock_svc):
        yield FlowProxyWebServerPlugin(**mock_plugin_args)


def make_mock_httpx_response(
    status_code: int = 200,
    reason_phrase: str = "OK",
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
    lines: list[str] | None = None,
) -> Mock:
    """Create a mock httpx.Response for testing.

    Use `chunks` for non-SSE iter_bytes() tests.
    Use `lines` for SSE iter_lines() tests (str values, empty string = event boundary).
    """
    response = Mock(spec=httpx.Response)
    response.status_code = status_code
    response.reason_phrase = reason_phrase
    response.headers = httpx.Headers(headers or {"content-type": "application/json"})
    response.iter_bytes.return_value = iter(chunks or [])
    response.iter_lines.return_value = iter(lines or [])
    return response


@contextmanager
def mock_httpx_stream(
    mock_svc: MagicMock,
    status_code: int = 200,
    reason_phrase: str = "OK",
    content_type: str = "application/json",
    chunks: list[bytes] | None = None,
) -> Generator[MagicMock, None, None]:
    """Configure mock_svc.http_client.stream for handle_request tests.

    Configures the http_client.stream() on the shared ProcessServices mock so
    that handle_request() exercises the happy path without hitting real backends.
    """
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.reason_phrase = reason_phrase
    mock_response.headers = httpx.Headers({"content-type": content_type})
    mock_response.iter_bytes.return_value = iter(chunks or [])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)

    mock_svc.http_client.stream.return_value = mock_response
    yield mock_response


class TestFlowProxyWebServerPluginInitialization:
    """Test FlowProxyWebServerPlugin initialization."""

    def test_plugin_initialization_success(
        self, mock_plugin_args: dict[str, Any]
    ) -> None:
        """Test successful plugin initialization."""
        # Clear shared state before test
        from flow_proxy_plugin.utils.process_services import ProcessServices

        ProcessServices.reset()

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
        from flow_proxy_plugin.utils.process_services import ProcessServices

        ProcessServices.reset()

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


class TestSendResponseHeaders:
    """Test _send_response_headers with httpx.Response."""

    def test_sse_response_includes_cache_control_and_accel_buffering(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """SSE responses must include Cache-Control: no-cache and X-Accel-Buffering: no."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream", "transfer-encoding": "chunked"}
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        all_headers = b"".join(queued)
        assert b"Cache-Control: no-cache" in all_headers
        assert b"X-Accel-Buffering: no" in all_headers

    def test_sse_response_preserves_transfer_encoding(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """SSE responses must NOT strip Transfer-Encoding: chunked."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream", "transfer-encoding": "chunked"}
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        all_headers = b"".join(queued)
        assert b"transfer-encoding: chunked" in all_headers.lower()

    def test_non_sse_response_has_no_sse_headers(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Non-SSE responses must NOT include SSE-specific headers."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "application/json", "content-length": "42"}
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        all_headers = b"".join(queued)
        assert b"Cache-Control: no-cache" not in all_headers
        assert b"X-Accel-Buffering: no" not in all_headers

    def test_uses_reason_phrase_not_reason(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Status line must use reason_phrase (httpx attr), not reason (requests attr)."""
        mock_response = make_mock_httpx_response(
            status_code=201, reason_phrase="Created"
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        status_line = bytes(queued[0])
        assert b"201 Created" in status_line


class TestStreamResponseBody:
    """Test _stream_response_body with httpx.Response."""

    def test_forwards_all_chunks_immediately(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Each chunk from iter_bytes() is queued immediately — no buffering."""
        chunks = [b"data: token1\n\n", b"data: token2\n\n", b"data: [DONE]\n\n"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        queued_chunks: list[bytes] = []
        plugin.client = Mock(
            queue=lambda mv: queued_chunks.append(bytes(mv)),
            connection=Mock(),
        )

        plugin._stream_response_body(mock_response)

        assert queued_chunks == chunks

    def test_stops_when_client_disconnects(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Streaming stops gracefully when client connection is lost."""
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        plugin.client = Mock(queue=Mock(), connection=None)  # disconnected

        bytes_sent, chunks_sent = plugin._stream_response_body(mock_response)

        # No chunks should be sent (client was disconnected from the start)
        assert chunks_sent == 0

    def test_handles_broken_pipe_gracefully(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """BrokenPipeError during streaming exits cleanly without raising."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        call_count = 0

        def queue_with_error(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise BrokenPipeError()

        plugin.client = Mock(queue=queue_with_error, connection=Mock())

        # Must not raise
        plugin._stream_response_body(mock_response)

    def test_handles_connection_reset_error_gracefully(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """ConnectionResetError during streaming exits cleanly without raising."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        call_count = 0

        def queue_with_reset(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise ConnectionResetError()

        plugin.client = Mock(queue=queue_with_reset, connection=Mock())

        plugin._stream_response_body(mock_response)  # must not raise

    def test_handles_os_error_errno_32_gracefully(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """OSError with errno=32 (broken pipe) exits cleanly without raising."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        call_count = 0

        def queue_with_oserror(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                err = OSError("Broken pipe")
                err.errno = 32
                raise err

        plugin.client = Mock(queue=queue_with_oserror, connection=Mock())

        plugin._stream_response_body(mock_response)  # must not raise

    def test_skips_empty_chunks(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Empty byte strings from iter_bytes() are skipped, not forwarded."""
        chunks = [b"chunk1", b"", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        queued_chunks: list[bytes] = []
        plugin.client = Mock(
            queue=lambda mv: queued_chunks.append(bytes(mv)),
            connection=Mock(),
        )

        plugin._stream_response_body(mock_response)

        assert queued_chunks == [b"chunk1", b"chunk2"]


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
    """Test handle_request with httpx mock."""

    def test_handle_request_success_queues_response(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Successful request queues status line and body chunks."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b'{"model": "gpt-3.5-turbo"}'
        request.headers = {b"content-type": (b"application/json", b"")}

        with (
            mock_httpx_stream(mock_svc, chunks=[b'{"response": "ok"}']),
            patch.object(
                plugin,
                "_get_config_and_token",
                return_value=({"clientId": "test"}, "test-config", "test-jwt-token"),
            ),
            patch.object(ProcessServices, "get", return_value=mock_svc),
        ):
            plugin.handle_request(request)

        # Status line (200 OK) must be queued
        queued_calls = plugin.client.queue.call_args_list  # type: ignore[attr-defined]
        all_bytes = b"".join(bytes(c[0][0]) for c in queued_calls)
        assert b"200" in all_bytes

    def test_handle_request_failure_sends_error(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Exceptions during request processing trigger _send_error()."""
        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"
        request.headers = {}

        mock_svc.http_client.stream.side_effect = Exception("Connection failed")

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
        with (
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch.object(plugin, "_send_error") as mock_error,
        ):
            plugin.handle_request(request)
            mock_error.assert_called_once()

    def test_handle_request_timeout_configuration(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """httpx.Timeout must be configured with connect=30s and read=600s."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b"{}"
        request.headers = {}

        captured_timeout: httpx.Timeout | None = None

        def capture_stream(**kwargs: Any) -> MagicMock:
            nonlocal captured_timeout
            captured_timeout = kwargs.get("timeout")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.reason_phrase = "OK"
            mock_resp.headers = httpx.Headers({"content-type": "application/json"})
            mock_resp.iter_bytes.return_value = iter([])
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_svc.http_client.stream.side_effect = capture_stream

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
        with (
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
            patch.object(ProcessServices, "get", return_value=mock_svc),
        ):
            plugin.handle_request(request)

        assert isinstance(captured_timeout, httpx.Timeout)
        assert captured_timeout.connect == 30.0
        assert captured_timeout.read == 600.0

    def test_handle_request_remote_protocol_error_does_not_send_error(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """httpx.RemoteProtocolError is logged but does NOT trigger _send_error()."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/messages"
        request.body = b"{}"
        request.headers = {}

        mock_svc.http_client.stream.side_effect = httpx.RemoteProtocolError(
            "Server disconnected", request=Mock()
        )

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
        with (
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch.object(plugin, "_send_error") as mock_error,
        ):
            plugin.handle_request(request)
            mock_error.assert_not_called()

    def test_handle_request_with_buffer_body(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Request body read from buffer attribute when body attribute is absent."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.buffer = bytearray(b'{"model": "gpt-3.5-turbo"}')
        request.headers = {}
        delattr(request, "body")

        with (
            mock_httpx_stream(mock_svc),
            patch.object(
                plugin,
                "_get_config_and_token",
                return_value=({"clientId": "test"}, "test-config", "test-jwt-token"),
            ),
            patch.object(ProcessServices, "get", return_value=mock_svc),
        ):
            plugin.handle_request(request)

        # Status line must be queued — confirms happy path was taken, not error path
        queued_calls = plugin.client.queue.call_args_list  # type: ignore[attr-defined]
        all_bytes = b"".join(bytes(c[0][0]) for c in queued_calls)
        assert b"200" in all_bytes
