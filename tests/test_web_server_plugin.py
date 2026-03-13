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
        """If auth fails, handle_request sends error and returns without leaking fds."""
        mock_svc.load_balancer.get_next_config.side_effect = Exception("no config")

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin.handle_request(request)

        assert plugin._streaming_state is None
        # _send_error should have been called
        assert plugin.client.queue.called  # type: ignore[attr-defined]

    def test_handle_request_cleans_up_pipe_on_setup_failure(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """If threading.Thread.start() raises, pipe fds are closed and state is None."""
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

        with patch("flow_proxy_plugin.plugins.web_server_plugin.os.pipe", side_effect=capturing_pipe):
            with patch("threading.Thread.start", side_effect=RuntimeError("thread start failed")):
                with patch.object(ProcessServices, "get", return_value=mock_svc):
                    plugin.handle_request(request)

        assert plugin._streaming_state is None
        # _send_error should have been called for the setup failure
        assert plugin.client.queue.called  # type: ignore[attr-defined]
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
