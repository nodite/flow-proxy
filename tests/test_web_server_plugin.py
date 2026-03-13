"""Unit tests for FlowProxyWebServerPlugin."""

import queue
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
    StreamingState,
    StreamStats,
    _ResponseHeaders,
)
from flow_proxy_plugin.utils.process_services import ProcessServices


class TestDataStructures:
    def test_streaming_state_defaults(self) -> None:
        import os
        import threading

        pipe_r, pipe_w = os.pipe()
        try:
            state = StreamingState(
                pipe_r=pipe_r,
                pipe_w=pipe_w,
                chunk_queue=queue.Queue(),
                thread=None,
                cancel=threading.Event(),
                req_id="abc123",
                config_name="test-config",
            )
            assert state.headers_sent is False
            assert state.status_code == 0
            assert state.error is None
        finally:
            os.close(pipe_r)
            os.close(pipe_w)

    def test_response_headers_fields(self) -> None:
        h = _ResponseHeaders(
            status_code=200,
            reason_phrase="OK",
            headers={"content-type": "text/event-stream"},
            is_sse=True,
        )
        assert h.is_sse is True
        assert h.headers["content-type"] == "text/event-stream"


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
            headers={
                "content-type": "text/event-stream",
                "transfer-encoding": "chunked",
            }
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
            headers={
                "content-type": "text/event-stream",
                "transfer-encoding": "chunked",
            }
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
        plugin.client = Mock(queue=lambda mv: queued_chunks.append(bytes(mv)))

        stats = plugin._stream_response_body(mock_response)

        assert queued_chunks == chunks
        assert stats.chunks_sent == 3
        assert stats.bytes_sent == sum(len(c) for c in chunks)
        assert stats.completed is True
        assert stats.event_count == 0  # non-SSE path never increments event_count

    def test_stops_when_client_disconnects(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Streaming stops gracefully when client disconnects on first write."""
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        plugin.client = Mock(queue=Mock(side_effect=BrokenPipeError()))

        stats = plugin._stream_response_body(mock_response)

        assert stats.bytes_sent == 0
        assert stats.completed is False
        assert stats.end_time is not None

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

        plugin.client = Mock(queue=queue_with_error)

        stats = plugin._stream_response_body(mock_response)  # must not raise
        assert stats.completed is False

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

        plugin.client = Mock(queue=queue_with_reset)

        stats = plugin._stream_response_body(mock_response)  # must not raise
        assert stats.completed is False

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

        plugin.client = Mock(queue=queue_with_oserror)

        stats = plugin._stream_response_body(mock_response)  # must not raise
        assert stats.completed is False

    def test_skips_empty_chunks(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Empty byte strings from iter_bytes() are skipped, not forwarded."""
        chunks = [b"chunk1", b"", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        queued_chunks: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued_chunks.append(bytes(mv)))

        stats = plugin._stream_response_body(mock_response)

        assert queued_chunks == [b"chunk1", b"chunk2"]
        assert stats.chunks_sent == 2


class TestSseStreamStats:
    """Tests for _stream_response_body with SSE (text/event-stream) responses."""

    def test_sse_stream_stats_event_count(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Empty lines in iter_lines() mark event boundaries and increment event_count."""
        # Two SSE events: "data: tok1\n\n" and "data: tok2\n\n"
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.event_count == 2
        assert stats.completed is True

    def test_sse_stream_stats_chunks_sent_excludes_separators(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """chunks_sent counts non-empty lines only (not blank event separators)."""
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.chunks_sent == 2  # two non-empty lines

    def test_sse_stream_stats_bytes_sent_includes_separators(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """bytes_sent includes both data lines and blank separator newlines."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        stats = plugin._stream_response_body(mock_response)

        total_queued = sum(len(b) for b in queued)
        assert stats.bytes_sent == total_queued

    def test_sse_stream_stats_ttft(self, plugin: FlowProxyWebServerPlugin) -> None:
        """ttft_ms measures time from start_time to first non-empty line."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        with patch("flow_proxy_plugin.plugins.web_server_plugin.time") as mock_time:
            mock_time.perf_counter.side_effect = iter([1000.0, 1000.042, 1003.292])
            stats = plugin._stream_response_body(mock_response)

        assert stats.ttft_ms is not None
        assert abs(stats.ttft_ms - 42.0) < 1.0

    def test_sse_stream_stats_duration(self, plugin: FlowProxyWebServerPlugin) -> None:
        """duration_ms measures time from first data to stream end."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        # start=1000.0, first_chunk=1000.042, end=1003.292 → duration=3250ms
        with patch("flow_proxy_plugin.plugins.web_server_plugin.time") as mock_time:
            mock_time.perf_counter.side_effect = iter([1000.0, 1000.042, 1003.292])
            stats = plugin._stream_response_body(mock_response)

        assert stats.duration_ms is not None
        assert abs(stats.duration_ms - 3250.0) < 1.0

    def test_sse_stream_interrupted_on_broken_pipe(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """BrokenPipeError during SSE sets completed=False, end_time is set."""
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        call_count = 0

        def queue_with_error(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise BrokenPipeError()

        plugin.client = Mock(queue=queue_with_error)

        stats = plugin._stream_response_body(mock_response)  # must not raise

        assert stats.completed is False
        assert stats.end_time is not None
        assert stats.bytes_sent > 0

    def test_non_sse_stream_stats(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Non-SSE path: event_count stays 0, bytes_sent and completed are correct."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(
            chunks=chunks
        )  # default: application/json

        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.event_count == 0
        assert stats.bytes_sent == sum(len(c) for c in chunks)
        assert stats.completed is True

    def test_sse_empty_stream(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Empty SSE stream (zero lines): completed=True, no data counters incremented."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=[],
        )
        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.completed is True
        assert stats.event_count == 0
        assert stats.chunks_sent == 0
        assert stats.bytes_sent == 0
        assert stats.ttft_ms is None

    def test_sse_interrupted_before_first_data(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """BrokenPipeError on first queue call: completed=False, bytes_sent==0, end_time set."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock(side_effect=BrokenPipeError()))

        stats = plugin._stream_response_body(mock_response)  # must not raise

        assert stats.completed is False
        assert stats.bytes_sent == 0
        assert stats.end_time is not None

    def test_sse_oserror_non_32_logs_warning(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """OSError with errno != 32 triggers warning log, not debug, and stops streaming."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        err = OSError("Input/output error")
        err.errno = 5
        plugin.client = Mock(queue=Mock(side_effect=err))

        with patch.object(plugin.logger, "warning") as mock_warning:
            stats = plugin._stream_response_body(mock_response)  # must not raise

        mock_warning.assert_called_once()
        assert stats.completed is False


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
            patch.object(
                plugin, "_get_config_and_token", return_value=mock_token_result
            ),
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
            patch.object(
                plugin, "_get_config_and_token", return_value=mock_token_result
            ),
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
            patch.object(
                plugin, "_get_config_and_token", return_value=mock_token_result
            ),
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


class TestHelperMethods:
    def test_encode_sse_line_empty_string_gives_newline(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        result = plugin._encode_sse_line("")
        assert result == b"\n"

    def test_encode_sse_line_content_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        result = plugin._encode_sse_line("data: hello")
        assert result == b"data: hello\n"

    def test_send_response_headers_from_status_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"content-type": "application/json"}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert b"HTTP/1.1 200 OK\r\n" in calls

    def test_send_response_headers_from_strips_connection(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"connection": "keep-alive", "content-type": "text/plain"}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert not any(b"connection" in c.lower() for c in calls)

    def test_send_response_headers_from_strips_transfer_encoding_for_non_sse(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"transfer-encoding": "chunked"}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert not any(b"transfer-encoding" in c.lower() for c in calls)

    def test_send_response_headers_from_keeps_transfer_encoding_for_sse(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"transfer-encoding": "chunked", "content-type": "text/event-stream"}, True)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert any(b"transfer-encoding" in c.lower() for c in calls)

    def test_send_response_headers_from_adds_sse_anti_buffer_headers(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"content-type": "text/event-stream"}, True)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert b"Cache-Control: no-cache\r\n" in calls
        assert b"X-Accel-Buffering: no\r\n" in calls

    def test_send_response_headers_from_ends_with_blank_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert calls[-1] == b"\r\n"


class TestStreamingWorker:
    """Tests for _streaming_worker() background thread method."""

    def _make_state(self) -> "StreamingState":
        import os
        import queue as q
        import threading

        pipe_r, pipe_w = os.pipe()
        return StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=q.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="test01",
            config_name="cfg",
        )

    def test_worker_puts_headers_then_chunks_then_sentinel(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Worker puts _ResponseHeaders, byte chunks, then None sentinel in order."""
        import os
        state = self._make_state()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.iter_bytes.return_value = iter([b"chunk1", b"chunk2"])
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        items = []
        while not state.chunk_queue.empty():
            items.append(state.chunk_queue.get_nowait())

        assert isinstance(items[0], _ResponseHeaders)
        assert items[1] == b"chunk1"
        assert items[2] == b"chunk2"
        assert items[-1] is None  # sentinel
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_sse_encodes_lines(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """SSE lines are encoded: empty string → b'\\n', content → bytes with \\n."""
        import os
        state = self._make_state()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"content-type": "text/event-stream"})
        mock_response.iter_lines.return_value = iter(["data: hi", "", "data: bye"])
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("POST", "https://example.com", {}, None, state)

        items = []
        while not state.chunk_queue.empty():
            items.append(state.chunk_queue.get_nowait())

        assert isinstance(items[0], _ResponseHeaders)  # headers
        assert items[1] == b"data: hi\n"
        assert items[2] == b"\n"          # SSE event boundary
        assert items[3] == b"data: bye\n"
        assert items[-1] is None
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_cancel_stops_iteration(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """cancel_event causes worker to break out of the iteration loop."""
        import os

        state = self._make_state()
        state.cancel.set()  # pre-set cancel before worker starts

        def slow_iter() -> "Generator[bytes, None, None]":
            yield b"first"
            yield b"should not appear"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.iter_bytes.return_value = slow_iter()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        items = []
        while not state.chunk_queue.empty():
            items.append(state.chunk_queue.get_nowait())

        # First chunk may or may not be present (cancel checked before enqueue),
        # but sentinel must be last and "should not appear" must not be present.
        assert items[-1] is None
        byte_items = [i for i in items if isinstance(i, bytes)]
        assert b"should not appear" not in byte_items
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_transport_error_sets_state_error_and_sentinel(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """On httpx.TransportError, worker stores error, marks client dirty, puts sentinel."""
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = httpx.TransportError("conn failed")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, httpx.TransportError)
        assert state.chunk_queue.get_nowait() is None  # sentinel
        mock_svc.mark_http_client_dirty.assert_called_once()
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_generic_exception_sets_error_and_sentinel(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = RuntimeError("boom")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, RuntimeError)
        assert state.chunk_queue.get_nowait() is None
        os.close(state.pipe_r)
        os.close(state.pipe_w)


class TestEventLoopHooks:
    """Tests for get_descriptors, read_from_descriptors, _finish_stream, _reset_request_state."""

    def _make_state_with_pipe(self) -> tuple["StreamingState", int, int]:
        import os
        import queue as q
        import threading

        pipe_r, pipe_w = os.pipe()
        state = StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=q.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="abc",
            config_name="cfg",
        )
        return state, pipe_r, pipe_w

    def test_get_descriptors_empty_when_no_state(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        plugin._streaming_state = None
        r, w = asyncio.run(plugin.get_descriptors())
        assert r == []
        assert w == []

    def test_get_descriptors_returns_pipe_r_when_streaming(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        plugin._streaming_state = state
        try:
            r, w = asyncio.run(plugin.get_descriptors())
            assert r == [pipe_r]
            assert w == []
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_noop_when_pipe_not_in_readables(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        plugin._streaming_state = state
        try:
            result = asyncio.run(
                plugin.read_from_descriptors([999])  # wrong fd
            )
            assert result is False
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_queues_each_chunk(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, False))
        state.chunk_queue.put(b"hello")
        state.chunk_queue.put(b"world")
        plugin._streaming_state = state
        # Write notification bytes so os.read doesn't block
        os.write(pipe_w, b"\x00\x00\x00")
        try:
            result = asyncio.run(
                plugin.read_from_descriptors([pipe_r])
            )
            assert result is False
            # _send_response_headers_from({}, not SSE) queues: status line + blank line = 2 calls
            # Plus 2 byte chunks = 4 total
            assert plugin.client.queue.call_count == 4  # type: ignore[attr-defined]
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_sends_headers_on_first_item(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_send_response_headers_from is called exactly once, before any byte chunks."""
        import asyncio
        import os
        from unittest.mock import call, patch

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, False))
        state.chunk_queue.put(b"first-chunk")
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00\x00")
        try:
            with patch.object(plugin, "_send_response_headers_from") as mock_send_headers:
                asyncio.run(plugin.read_from_descriptors([pipe_r]))
            # Headers sent exactly once
            assert mock_send_headers.call_count == 1
            # Headers sent before the byte chunk (queue called once for the chunk)
            assert plugin.client.queue.call_count == 1  # type: ignore[attr-defined]
            assert plugin.client.queue.call_args == call(memoryview(b"first-chunk"))  # type: ignore[attr-defined]
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_returns_true_on_sentinel(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.status_code = 200
        state.config_name = "cfg"
        state.chunk_queue.put(None)  # sentinel only
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        result = asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        assert result is True
        assert plugin._streaming_state is None  # _finish_stream cleared it

    def test_get_descriptors_empty_after_stream_finishes(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """After sentinel is processed, get_descriptors() returns []."""
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.status_code = 200
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        r, w = asyncio.run(plugin.get_descriptors())
        assert r == []

    def test_reset_request_state_sets_cancel_and_closes_fds(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import os
        import threading

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        state.thread = t
        plugin._streaming_state = state
        plugin._reset_request_state()
        assert state.cancel.is_set()
        assert plugin._streaming_state is None
        # fds should be closed — attempting to close them again should raise OSError
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_reset_is_idempotent_when_state_is_none(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        plugin._streaming_state = None
        plugin._reset_request_state()  # should not raise

    def test_reset_is_safe_when_join_times_out(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_reset_request_state closes fds even when join() times out (blocked worker)."""
        import os
        import threading

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        # Thread that blocks until cancel is set — simulates unresponsive upstream
        block = threading.Event()
        t = threading.Thread(target=lambda: block.wait(timeout=60), daemon=True)
        t.start()
        state.thread = t
        plugin._streaming_state = state
        # join(timeout=2.0) would block for 2s; patch it to return immediately
        from unittest.mock import patch  # noqa: PLC0415

        with patch.object(t, "join", return_value=None):
            plugin._reset_request_state()
        # fds must be closed regardless of join timeout
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)
        assert plugin._streaming_state is None
        block.set()  # let daemon thread exit cleanly

    def test_worker_error_before_headers_sends_503(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("boom")
        state.headers_sent = False
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        # _send_error should have been called — client.queue should contain error response
        assert plugin.client.queue.called  # type: ignore[attr-defined]

    def test_worker_error_after_headers_does_not_send_error_response(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("boom after headers")
        state.headers_sent = True
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        plugin.client.queue.reset_mock()  # type: ignore[attr-defined]
        asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        # No error response queued when headers already sent
        assert not plugin.client.queue.called  # type: ignore[attr-defined]
        # Error must be logged (warning-level when headers already committed)
        plugin.logger.warning.assert_called()  # type: ignore[attr-defined]
