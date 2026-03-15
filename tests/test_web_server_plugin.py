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
                start_time=0.0,
                stream=None,
            )
            assert state.headers_sent is False
            assert state.status_code == 0
            assert state.error is None
            assert state.ttfb is None        # new
            assert state.bytes_sent == 0     # new
            assert state.end_reason == ""
        finally:
            os.close(pipe_r)
            os.close(pipe_w)

    def test_streaming_state_new_fields(self) -> None:
        import os
        import threading
        import time

        pipe_r, pipe_w = os.pipe()
        try:
            t = time.time()
            state = StreamingState(
                pipe_r=pipe_r, pipe_w=pipe_w,
                chunk_queue=queue.Queue(),
                thread=None,
                cancel=threading.Event(),
                req_id="abc123",
                config_name="test-config",
                start_time=t,
                stream=True,
            )
            assert state.start_time == t
            assert state.stream is True
            assert state.ttfb is None
            assert state.bytes_sent == 0
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
        assert b"500 Internal Server Error" in response_bytes
        assert b"Internal server error" in response_bytes

    def test_send_error_custom_uses_fallback_for_unknown_code(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Status codes not in _REASON_PHRASES fall back to 'Error' as reason phrase.

        NOTE: 404 is intentionally NOT in _REASON_PHRASES; this test validates the
        fallback path. If 404 is ever added to _REASON_PHRASES, update this test
        to use a different uncovered code (e.g. 418).
        """
        mock_queue = Mock()
        plugin.client = Mock(queue=mock_queue)

        plugin._send_error(status_code=404, message="Not found")

        mock_queue.assert_called_once()
        call_args = mock_queue.call_args[0][0]
        response_bytes = bytes(call_args)
        assert b"404 Error" in response_bytes  # fallback reason phrase
        assert b"Not found" in response_bytes


class TestHandleRequest:
    """Test handle_request with httpx mock."""

    def test_handle_request_timeout_configuration(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Worker passes httpx.Timeout(connect=30s, read=600s) to stream()."""
        import threading
        captured_timeout: list[Any] = []
        stream_called = threading.Event()

        def capture_stream(**kwargs: Any) -> MagicMock:
            captured_timeout.append(kwargs.get("timeout"))
            stream_called.set()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.reason_phrase = "OK"
            mock_resp.headers = httpx.Headers({"content-type": "application/json"})
            mock_resp.iter_bytes.return_value = iter([])
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_svc.http_client.stream.side_effect = capture_stream

        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b"{}"
        request.headers = {}

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
        with (
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
            patch.object(ProcessServices, "get", return_value=mock_svc),
        ):
            plugin.handle_request(request)

        # Wait for worker thread to call stream()
        assert stream_called.wait(timeout=2.0), "Worker did not call stream() in time"
        assert isinstance(captured_timeout[0], httpx.Timeout)
        assert captured_timeout[0].connect == 30.0
        assert captured_timeout[0].read == 600.0
        # Clean up: stop the streaming state
        if plugin._streaming_state:
            plugin._reset_request_state()


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

    def test_send_response_headers_from_strips_transfer_encoding_for_sse(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """We stream raw bytes, not chunked framing; client would fail on raw SSE otherwise."""
        h = _ResponseHeaders(200, "OK", {"transfer-encoding": "chunked", "content-type": "text/event-stream"}, True)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]  # type: ignore[attr-defined]
        assert not any(b"transfer-encoding" in c.lower() for c in calls)

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


class TestParseStreamField:
    """Direct unit tests for FlowProxyWebServerPlugin._parse_stream_field()."""

    def test_stream_true_from_json_body(self) -> None:
        request = MagicMock(spec=HttpParser)
        request.body = b'{"stream": true}'
        request.buffer = None
        assert FlowProxyWebServerPlugin._parse_stream_field(request) is True

    def test_stream_false_from_json_body(self) -> None:
        request = MagicMock(spec=HttpParser)
        request.body = b'{"stream": false}'
        request.buffer = None
        assert FlowProxyWebServerPlugin._parse_stream_field(request) is False

    def test_no_stream_field_returns_none(self) -> None:
        request = MagicMock(spec=HttpParser)
        request.body = b'{"model": "claude-3-5-sonnet"}'
        request.buffer = None
        assert FlowProxyWebServerPlugin._parse_stream_field(request) is None

    def test_absent_body_returns_none(self) -> None:
        request = MagicMock(spec=HttpParser)
        request.body = None
        request.buffer = None
        assert FlowProxyWebServerPlugin._parse_stream_field(request) is None

    def test_non_json_body_returns_none(self) -> None:
        request = MagicMock(spec=HttpParser)
        request.body = b"not json at all"
        request.buffer = None
        assert FlowProxyWebServerPlugin._parse_stream_field(request) is None

    def test_non_dict_json_body_returns_none(self) -> None:
        """Valid JSON but not an object (list, int, null) must return None, not raise."""
        request = MagicMock(spec=HttpParser)
        request.buffer = None
        for non_dict_body in (b"[1,2,3]", b"42", b'"hello"', b"null"):
            request.body = non_dict_body
            assert FlowProxyWebServerPlugin._parse_stream_field(request) is None, (
                f"Expected None for body={non_dict_body!r}"
            )

    def test_buffer_fallback_when_body_absent(self) -> None:
        request = MagicMock(spec=HttpParser)
        request.body = None
        request.buffer = bytearray(b'{"stream": true}')
        assert FlowProxyWebServerPlugin._parse_stream_field(request) is True


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
            start_time=0.0,
            stream=None,
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
        """On httpx.TransportError, worker stores error, sets end_reason, puts sentinel; does NOT mark client dirty."""
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = httpx.TransportError("conn failed")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, httpx.TransportError)
        assert state.end_reason == "transport_error"
        assert state.chunk_queue.get_nowait() is None  # sentinel
        mock_svc.mark_http_client_dirty.assert_not_called()
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_remote_protocol_error_no_dirty(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """RemoteProtocolError sets end_reason='remote_closed' and does NOT mark client dirty."""
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = httpx.RemoteProtocolError("peer closed")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, httpx.RemoteProtocolError)
        assert state.end_reason == "remote_closed"
        assert state.chunk_queue.get_nowait() is None  # sentinel present
        mock_svc.mark_http_client_dirty.assert_not_called()
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_connect_error_no_dirty(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """ConnectError sets end_reason='connect_error' and does NOT mark client dirty."""
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = httpx.ConnectError("refused")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, httpx.ConnectError)
        assert state.end_reason == "connect_error"
        assert state.chunk_queue.get_nowait() is None
        mock_svc.mark_http_client_dirty.assert_not_called()
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

    def test_worker_logs_backend_line_with_ttfb(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Worker emits 'backend=200 transfer=chunked ttfb=Xs' on first chunk."""
        import os
        import time
        state = self._make_state()
        state.start_time = time.time() - 0.5  # simulate 500ms before first chunk
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"transfer-encoding": "chunked"})
        mock_response.iter_bytes.return_value = iter([b"hello"])
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response
        plugin.logger = MagicMock()

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("POST", "https://example.com", {}, None, state)

        # Find the backend= info call
        info_calls = plugin.logger.info.call_args_list
        backend_calls = [c for c in info_calls if c.args and str(c.args[0]).startswith("backend=")]
        assert len(backend_calls) == 1
        backend_str = backend_calls[0].args[0] % backend_calls[0].args[1:]
        assert "backend=200" in backend_str
        assert "transfer=chunked" in backend_str
        assert "ttfb=" in backend_str
        # Old lines must NOT appear
        old_calls = [c for c in info_calls if c.args and "Backend response:" in str(c.args[0])]
        assert old_calls == []
        old_first = [c for c in info_calls if c.args and "Received first" in str(c.args[0])]
        assert old_first == []
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_sets_state_ttfb(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Worker sets state.ttfb after receiving the first chunk."""
        import os
        import time
        state = self._make_state()
        state.start_time = time.time()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({})
        mock_response.iter_bytes.return_value = iter([b"data"])
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert state.ttfb is not None
        assert state.ttfb >= 0.0
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
            start_time=0.0,
            stream=None,
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

    def test_read_from_descriptors_tracks_bytes_sent(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """bytes_sent accumulates payload bytes; header bytes are excluded."""
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        plugin._streaming_state = state
        plugin.client = MagicMock()

        # Pre-load queue: headers item then two byte chunks
        state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, False))
        state.chunk_queue.put(b"hello")
        state.chunk_queue.put(b"world!")

        os.write(pipe_w, b"\x00")  # trigger read
        try:
            asyncio.run(plugin.read_from_descriptors([pipe_r]))
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

        assert state.bytes_sent == len(b"hello") + len(b"world!")  # 11

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
        state.is_sse = False  # explicit: tests non-SSE silent-close path
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

    def test_is_sse_propagated_to_state_when_response_headers_processed(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """read_from_descriptors sets state.is_sse=True from _ResponseHeaders(is_sse=True)."""
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        assert state.is_sse is False  # default
        state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, True))  # is_sse=True
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        try:
            asyncio.run(plugin.read_from_descriptors([pipe_r]))
        finally:
            plugin._streaming_state = None
            for fd in (pipe_r, pipe_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
        assert state.is_sse is True

    def test_sse_error_event_sent_when_error_after_headers_on_sse_stream(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream injects SSE error event when error occurs after headers on SSE stream."""
        import asyncio
        import json
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("upstream boom")
        state.headers_sent = True
        state.is_sse = True
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        plugin.client.queue.reset_mock()  # type: ignore[attr-defined]
        asyncio.run(plugin.read_from_descriptors([pipe_r]))

        # Must have queued exactly one item — the SSE error event
        assert plugin.client.queue.call_count == 1  # type: ignore[attr-defined]
        queued = bytes(plugin.client.queue.call_args[0][0])  # type: ignore[attr-defined]
        expected_payload = json.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": "Upstream connection lost"},
        })
        expected = f"event: error\ndata: {expected_payload}\n\n".encode()
        assert queued == expected

    def test_non_sse_error_after_headers_still_silent(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream does NOT send anything when error occurs after headers on non-SSE stream."""
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("upstream boom")
        state.headers_sent = True
        state.is_sse = False  # explicit — non-SSE path
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        plugin.client.queue.reset_mock()  # type: ignore[attr-defined]
        asyncio.run(plugin.read_from_descriptors([pipe_r]))

        assert not plugin.client.queue.called  # type: ignore[attr-defined]
        plugin.logger.warning.assert_called()  # type: ignore[attr-defined]

    def test_error_before_headers_sends_503_bytes(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Regression guard: _finish_stream sends a 503 response when error precedes headers."""
        import asyncio
        import os

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("pre-header boom")
        state.headers_sent = False
        state.is_sse = False
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        asyncio.run(plugin.read_from_descriptors([pipe_r]))

        assert plugin.client.queue.called  # type: ignore[attr-defined]
        queued = bytes(plugin.client.queue.call_args_list[0][0][0])  # type: ignore[attr-defined]
        assert b"503" in queued


    def test_reset_request_state_emits_client_disconnected_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_reset_request_state emits '← 200 [cfg] ... end=client_disconnected' at INFO."""
        import os
        import threading
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 8.0
        state.stream = True
        state.ttfb = 2.0
        state.bytes_sent = 512
        state.status_code = 200
        plugin._streaming_state = state
        plugin.logger = MagicMock()

        # Assign a dummy thread so join() doesn't fail
        state.thread = threading.Thread(target=lambda: None)
        state.thread.start()

        plugin._reset_request_state()

        info_calls = plugin.logger.info.call_args_list
        completion = [c for c in info_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "← 200 [cfg]" in msg
        assert "stream=True" in msg
        assert "ttfb=2.0s" in msg
        assert "bytes=512" in msg
        assert "end=client_disconnected" in msg
        # Old freeform line must NOT appear
        old = [c for c in info_calls if c.args and "Stream canceled" in str(c.args[0])]
        assert old == []
        # _reset_request_state closes both fds; re-closing must raise OSError
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_finish_stream_ok_emits_structured_completion_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream emits '← 200 [cfg] stream=True ttfb=1.2s ... end=ok' at INFO.

        _finish_stream closes both fds, so re-closing raises OSError.
        """
        import os
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 5.0
        state.stream = True
        state.ttfb = 1.2
        state.bytes_sent = 1024
        state.status_code = 200
        state.headers_sent = True
        plugin._streaming_state = state
        plugin.client = MagicMock()
        plugin.logger = MagicMock()

        plugin._finish_stream(state)

        info_calls = plugin.logger.info.call_args_list
        completion = [c for c in info_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "← 200 [cfg]" in msg
        assert "stream=True" in msg
        assert "ttfb=1.2s" in msg
        assert "bytes=1024" in msg
        assert "end=ok" in msg
        # _finish_stream closes both fds; re-closing must raise OSError
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_finish_stream_transport_error_emits_warning(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream emits WARNING with end=remote_closed when state.end_reason='remote_closed'.

        state.headers_sent=False causes _send_error(503) to be called (client.queue mock absorbs it).
        """
        import os
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 10.0
        state.stream = False
        state.ttfb = None
        state.bytes_sent = 0
        state.status_code = 0
        state.headers_sent = False
        state.error = httpx.RemoteProtocolError("peer closed")
        state.end_reason = "remote_closed"
        plugin._streaming_state = state
        plugin.client = MagicMock()
        plugin.logger = MagicMock()

        plugin._finish_stream(state)

        warning_calls = plugin.logger.warning.call_args_list
        completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "end=remote_closed" in msg
        assert "ttfb=-" in msg
        # Old "Stream ended with error" line must NOT appear
        old = [c for c in warning_calls if c.args and "Stream ended with error" in str(c.args[0])]
        assert old == []
        # _finish_stream closes both fds; re-closing must raise OSError
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_finish_stream_end_reason_remote_closed(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream logs end=remote_closed when state.end_reason='remote_closed'."""
        import os
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 10.0
        state.stream = True
        state.ttfb = 3.9
        state.bytes_sent = 2657
        state.status_code = 200
        state.headers_sent = True
        state.is_sse = False
        state.error = httpx.RemoteProtocolError("peer closed")
        state.end_reason = "remote_closed"
        plugin._streaming_state = state
        plugin.client = MagicMock()
        plugin.logger = MagicMock()

        plugin._finish_stream(state)

        warning_calls = plugin.logger.warning.call_args_list
        completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "end=remote_closed" in msg
        # _finish_stream closes both fds; re-closing must raise OSError
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_finish_stream_end_reason_connect_error(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream logs end=connect_error when state.end_reason='connect_error'."""
        import os
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 1.0
        state.stream = True
        state.ttfb = None
        state.bytes_sent = 0
        state.status_code = 0
        state.headers_sent = False
        state.is_sse = False
        state.error = httpx.ConnectError("refused")
        state.end_reason = "connect_error"
        plugin._streaming_state = state
        plugin.client = MagicMock()
        plugin.logger = MagicMock()

        plugin._finish_stream(state)

        warning_calls = plugin.logger.warning.call_args_list
        completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "end=connect_error" in msg
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_finish_stream_end_reason_transport_error(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream logs end=transport_error when state.end_reason='transport_error' (generic fallback)."""
        import os
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 5.0
        state.stream = True
        state.ttfb = 1.2
        state.bytes_sent = 1024
        state.status_code = 200
        state.headers_sent = True
        state.is_sse = False
        state.error = httpx.TransportError("unknown")
        state.end_reason = "transport_error"
        plugin._streaming_state = state
        plugin.client = MagicMock()
        plugin.logger = MagicMock()

        plugin._finish_stream(state)

        warning_calls = plugin.logger.warning.call_args_list
        completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "end=transport_error" in msg
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_finish_stream_worker_error_emits_warning(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_finish_stream emits WARNING with end=worker_error for non-TransportError exceptions."""
        import os
        import time

        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.start_time = time.time() - 5.0
        state.stream = True
        state.ttfb = 1.2
        state.bytes_sent = 64
        state.status_code = 200
        state.headers_sent = True
        state.is_sse = False
        state.error = RuntimeError("unexpected db error")
        plugin._streaming_state = state
        plugin.client = MagicMock()
        plugin.logger = MagicMock()

        plugin._finish_stream(state)

        warning_calls = plugin.logger.warning.call_args_list
        completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
        assert len(completion) == 1
        msg = completion[0].args[0] % completion[0].args[1:]
        assert "end=worker_error" in msg
        assert "ttfb=1.2s" in msg
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)


class TestHandleRequestAsync:
    """Tests for the refactored handle_request() that starts a worker and returns."""

    def test_handle_request_starts_worker_thread(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """handle_request() returns immediately and sets _streaming_state."""
        mock_svc.load_balancer.get_next_config.return_value = {
            "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
        }
        mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
        mock_svc.request_filter.find_matching_rule.return_value = None
        mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

        # Make http_client.stream block until we release it
        import threading
        ready = threading.Event()
        released = threading.Event()

        def blocking_stream(*a: Any, **kw: Any) -> MagicMock:
            ready.set()
            released.wait(timeout=2)
            return MagicMock(__enter__=MagicMock(return_value=MagicMock(
                status_code=200, reason_phrase="OK",
                headers=httpx.Headers({"content-type": "application/json"}),
                iter_bytes=MagicMock(return_value=iter([])),
            )), __exit__=MagicMock(return_value=False))

        mock_svc.http_client.stream.side_effect = blocking_stream

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin.handle_request(request)

        # handle_request() returned — state should be set
        assert plugin._streaming_state is not None
        released.set()  # let the worker finish

    def test_handle_request_cleans_up_pipe_on_auth_failure(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """If auth fails, handle_request sends 503, clears context, and returns without leaking state."""
        mock_svc.load_balancer.get_next_config.side_effect = Exception("no config")

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        with (
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch(
                "flow_proxy_plugin.plugins.web_server_plugin.clear_request_context"
            ) as mock_clear,
        ):
            plugin.handle_request(request)

        assert plugin._streaming_state is None
        assert plugin.client.queue.called  # type: ignore[attr-defined]
        mock_clear.assert_called_once()  # cleanup invariant

    def test_handle_request_cleans_up_pipe_on_setup_failure(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """If threading.Thread.start() raises, pipe fds are closed, state is None, context cleared."""
        mock_svc.load_balancer.get_next_config.return_value = {
            "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
        }
        mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
        mock_svc.request_filter.find_matching_rule.return_value = None
        mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        import os
        captured_fds: list[int] = []
        real_pipe = os.pipe

        def capturing_pipe() -> tuple[int, int]:
            r, w = real_pipe()
            captured_fds.extend([r, w])
            return r, w

        with (
            patch("flow_proxy_plugin.plugins.web_server_plugin.os.pipe", side_effect=capturing_pipe),
            patch("threading.Thread.start", side_effect=RuntimeError("thread start failed")),
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch(
                "flow_proxy_plugin.plugins.web_server_plugin.clear_request_context"
            ) as mock_clear,
        ):
            plugin.logger = MagicMock()
            plugin.handle_request(request)

        assert plugin._streaming_state is None
        assert plugin.client.queue.called  # type: ignore[attr-defined]
        mock_clear.assert_called_once()  # cleanup invariant

        # §4.1: stream setup failure must log ERROR with exc_info=True
        plugin.logger.error.assert_called_once()
        error_call = str(plugin.logger.error.call_args)
        assert "Failed to start streaming" in error_call

        # Both pipe fds must have been closed — re-closing should raise OSError
        assert len(captured_fds) == 2
        with pytest.raises(OSError):
            os.close(captured_fds[0])
        with pytest.raises(OSError):
            os.close(captured_fds[1])

    def test_handle_request_filter_applied(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Filter rule is applied synchronously in handle_request() before thread start."""
        from flow_proxy_plugin.plugins.request_filter import FilterRule
        mock_svc.load_balancer.get_next_config.return_value = {
            "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
        }
        mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
        mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

        # Return a filter rule that removes a query param
        filter_rule = FilterRule(
            name="test-rule",
            matcher=lambda req, path: True,
            query_params_to_remove=["key"],
        )
        mock_svc.request_filter.find_matching_rule.return_value = filter_rule
        mock_svc.request_filter.filter_query_params.return_value = "/v1/messages"
        mock_svc.request_filter.get_headers_to_skip.return_value = set()

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/messages?key=secret"
        request.headers = {}
        request.body = None
        request.buffer = None

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin.handle_request(request)

        # filter_query_params must be called — proves filter was applied before thread start
        mock_svc.request_filter.filter_query_params.assert_called_once()
        # Clean up
        if plugin._streaming_state:
            plugin._reset_request_state()

    def test_entry_log_includes_stream_true(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """handle_request emits '→ POST /path stream=True' when body has stream=true."""
        request = MagicMock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/messages"
        request.body = b'{"stream": true}'
        request.buffer = None
        request.headers = {}
        mock_svc.request_filter.find_matching_rule.return_value = None
        plugin.logger = MagicMock()

        with (
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch.object(plugin, "_get_config_and_token", return_value=({"clientId": "cid"}, "test-cfg", "tok")),
        ):
            plugin.handle_request(request)

        # First INFO call is the entry line
        info_calls = plugin.logger.info.call_args_list
        entry_calls = [c for c in info_calls if c.args and str(c.args[0]).startswith("→ ") and "stream" in str(c)]
        assert len(entry_calls) >= 1
        entry_msg = entry_calls[0].args[0] % entry_calls[0].args[1:]
        assert "stream=True" in entry_msg
        # Clean up worker thread
        if plugin._streaming_state:
            plugin._reset_request_state()

    def test_entry_log_stream_none_when_no_body(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """handle_request emits 'stream=None' when body is absent."""
        request = MagicMock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"
        request.body = None
        request.buffer = None
        request.headers = {}
        mock_svc.request_filter.find_matching_rule.return_value = None
        plugin.logger = MagicMock()

        with (
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch.object(plugin, "_get_config_and_token", return_value=({"clientId": "cid"}, "test-cfg", "tok")),
        ):
            plugin.handle_request(request)

        info_calls = plugin.logger.info.call_args_list
        entry_calls = [c for c in info_calls if c.args and str(c.args[0]).startswith("→ ") and "stream" in str(c)]
        assert len(entry_calls) >= 1
        # stream arg should be None
        first_entry = entry_calls[0]
        assert first_entry.args[-1] is None  # last positional arg to logger.info is stream value
        if plugin._streaming_state:
            plugin._reset_request_state()

    def test_fwd_log_uses_arrow_format(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """handle_request emits '→ https://...' for the FWD line, not 'Sending request to backend:'."""
        request = MagicMock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/messages"
        request.body = b'{}'
        request.buffer = None
        request.headers = {}
        mock_svc.request_filter.find_matching_rule.return_value = None
        plugin.logger = MagicMock()

        with (
            patch.object(ProcessServices, "get", return_value=mock_svc),
            patch.object(plugin, "_get_config_and_token", return_value=({"clientId": "cid"}, "test-cfg", "tok")),
        ):
            plugin.handle_request(request)

        info_calls = plugin.logger.info.call_args_list
        fwd_calls = [c for c in info_calls if c.args and "flow.ciandt.com" in str(c)]
        assert len(fwd_calls) == 1
        assert fwd_calls[0].args[0] == "→ %s"
        old_calls = [c for c in info_calls if c.args and "Sending request to backend:" in str(c.args[0])]
        assert old_calls == []
        if plugin._streaming_state:
            plugin._reset_request_state()
