"""Web Server plugin for reverse proxy mode."""

import json
import logging
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from proxy.http.parser import HttpParser
from proxy.http.server import HttpWebServerBasePlugin, httpProtocolTypes

from ..utils.log_context import (
    clear_request_context,
    component_context,
    set_request_context,
)
from ..utils.plugin_pool import PluginPool
from ..utils.process_services import ProcessServices
from .base_plugin import BaseFlowProxyPlugin
from .request_filter import FilterRule

_web_pool: Optional["PluginPool[FlowProxyWebServerPlugin]"] = None
_web_pool_lock = threading.Lock()
_WEB_POOL_SIZE = int(os.getenv("FLOW_PROXY_PLUGIN_POOL_SIZE", "64"))


@dataclass
class _ResponseHeaders:
    """Response metadata passed from worker thread to main thread.

    Contains only plain Python types — no httpx objects cross thread boundaries.
    """

    status_code: int
    reason_phrase: str
    headers: dict[str, str]  # extracted from httpx.Headers
    is_sse: bool  # True if Content-Type: text/event-stream


@dataclass
class StreamingState:  # pylint: disable=too-many-instance-attributes
    """Per-request streaming state for the B3 async pipe-mediated pattern.

    Stored as self._streaming_state. Initialized to None in __init__ and _rebind().
    Thread-safety: self.client is ONLY touched by the main thread.
    state.error is written by worker before queue.put(None) and read by main
    thread after queue.get() returns None — GIL guarantees visibility in CPython.
    Same GIL guarantee applies to state.ttfb (written by worker before sentinel,
    read by main thread after sentinel is dequeued).
    state.bytes_sent is written only from the main thread (read_from_descriptors).
    """

    pipe_r: int  # registered with selector as readable fd
    pipe_w: int  # worker writes notification bytes here
    chunk_queue: "queue.Queue[_ResponseHeaders | bytes | None]"
    thread: threading.Thread | None  # None until assigned in handle_request()
    cancel: threading.Event  # set by _reset_request_state() on disconnect
    req_id: str  # for log context
    config_name: str  # for final access log line
    start_time: float  # set in handle_request() via time.time()
    stream: bool | None  # parsed from request body; None if indeterminate
    headers_sent: bool = False  # guards error-response logic
    is_sse: bool = False  # set by read_from_descriptors when _ResponseHeaders processed
    status_code: int = 0  # stored after _ResponseHeaders item is processed
    error: BaseException | None = None  # set by worker on exception
    ttfb: float | None = None  # set by worker on first chunk/line
    bytes_sent: int = 0  # incremented in read_from_descriptors() main thread only


class FlowProxyWebServerPlugin(HttpWebServerBasePlugin, BaseFlowProxyPlugin):
    """Flow LLM Proxy web server plugin for reverse proxy mode.

    This plugin handles direct HTTP requests (reverse proxy mode) and forwards
    them to Flow LLM Proxy with authentication.
    """

    _log_once: bool = False  # Class variable: log initialization message only once

    def __new__(  # pylint: disable=too-many-positional-arguments
        cls,
        uid: str,
        flags: Any,
        client: Any,
        event_queue: Any,
        upstream_conn_pool: Any = None,
    ) -> "FlowProxyWebServerPlugin":
        global _web_pool  # pylint: disable=global-statement
        if _web_pool is None:
            with _web_pool_lock:
                if _web_pool is None:
                    _web_pool = PluginPool(cls, max_size=_WEB_POOL_SIZE)
        return _web_pool.acquire(uid, flags, client, event_queue, upstream_conn_pool)

    def __init__(  # pylint: disable=too-many-positional-arguments
        self,
        uid: str,
        flags: Any,
        client: Any,
        event_queue: Any,
        upstream_conn_pool: Any = None,
    ) -> None:
        """Initialize web server plugin."""
        if self._pooled:
            return  # Reuse: _rebind() already ran in pool.acquire()
        # First-time initialization (runs exactly once per instance)
        HttpWebServerBasePlugin.__init__(
            self, uid, flags, client, event_queue, upstream_conn_pool
        )
        self._init_services()
        self._streaming_state: StreamingState | None = None
        self._pooled = True
        if not FlowProxyWebServerPlugin._log_once:
            FlowProxyWebServerPlugin._log_once = True
            self.logger.info("FlowProxyWebServerPlugin initialized (pooled)")

    def _rebind(  # pylint: disable=too-many-positional-arguments,arguments-differ
        self,
        uid: str,
        flags: Any,
        client: Any,
        event_queue: Any,
        upstream_conn_pool: Any = None,
    ) -> None:
        """Rebind proxy.py connection-specific state for pool reuse."""
        HttpWebServerBasePlugin.__init__(
            self, uid, flags, client, event_queue, upstream_conn_pool
        )
        self._pooled = True  # Ensure flag stays True
        self._streaming_state = None

    def on_client_connection_close(self) -> None:
        """Return instance to pool when connection closes."""
        if _web_pool is not None and self._pooled:
            _web_pool.release(self)

    async def get_descriptors(self) -> tuple[list[int], list[int]]:
        """Register pipe_r with proxy.py's selector while streaming is active."""
        if self._streaming_state is not None:
            return [self._streaming_state.pipe_r], []
        return [], []

    async def read_from_descriptors(self, r: list[int]) -> bool:
        """Drain chunk_queue when pipe_r is readable; queue chunks to self.client.

        Returns True (teardown signal) when the sentinel is processed.
        Invariant: only this method (main thread) calls self.client.queue().
        """
        state = self._streaming_state
        if state is None or state.pipe_r not in r:
            return False

        # Drain notification bytes (batch). More bytes than 256 cause harmless
        # re-entry on the next select cycle with an empty queue — self-correcting.
        os.read(state.pipe_r, 256)
        set_request_context(state.req_id, "WS")

        while not state.chunk_queue.empty():
            item = state.chunk_queue.get_nowait()

            if isinstance(item, _ResponseHeaders):
                state.is_sse = item.is_sse
                state.status_code = item.status_code
                self._send_response_headers_from(item)
                state.headers_sent = True

            elif item is None:  # sentinel — stream ended or errored
                self._finish_stream(state)
                return True  # signal proxy.py to close connection

            else:  # bytes chunk
                state.bytes_sent += len(item)
                self.client.queue(memoryview(item))

        return False

    def _finish_stream(self, state: StreamingState) -> None:
        """Close pipe fds, clear state, log completion. Called from main thread only."""
        # Clear first so get_descriptors() immediately returns [] on next call
        self._streaming_state = None
        for fd in (state.pipe_r, state.pipe_w):
            try:
                os.close(fd)
            except OSError:
                pass

        if state.error:
            if not state.headers_sent:
                self._send_error(503, "Upstream error")
            elif state.is_sse:
                self._send_sse_error_event()
            # non-SSE + headers sent: silent close (unchanged)
            log_func = self.logger.warning if state.headers_sent else self.logger.error
            log_func("Stream ended with error: %s", state.error)
        else:
            log_func = (
                self.logger.info if state.status_code < 400 else self.logger.warning
            )
            log_func("← %d [%s]", state.status_code, state.config_name)
        clear_request_context()
        # metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)

    def _reset_request_state(self) -> None:
        """Cancel and join worker thread; close pipe fds. Called by PluginPool.release()."""
        state = self._streaming_state
        if state is None:
            return
        set_request_context(state.req_id, "WS")
        self.logger.info(
            "Stream canceled (client disconnect), no ← 200 [%s]", state.config_name
        )
        state.cancel.set()
        if state.thread is not None:
            state.thread.join(timeout=2.0)
            # If join times out, thread is abandoned as daemon; it holds no reference
            # to self.client so no further socket writes occur.
        for fd in (state.pipe_r, state.pipe_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._streaming_state = None
        clear_request_context()

    def routes(self) -> list[tuple[int, str]]:
        """Define routes that this plugin handles."""
        return [(httpProtocolTypes.HTTP, r"/.*")]

    def handle_request(self, request: HttpParser) -> None:  # pylint: disable=too-many-locals
        """Start async streaming: authenticate, build params, launch worker, return.

        Does NOT block waiting for the upstream response. The worker thread feeds
        chunks through StreamingState; read_from_descriptors() delivers them to
        the client via proxy.py's event loop.
        """
        method = self._decode_bytes(request.method) if request.method else "GET"
        path = self._decode_bytes(request.path) if request.path else "/"

        req_id = secrets.token_hex(3)
        set_request_context(req_id, "WS")
        start_time = time.time()
        stream = self._parse_stream_field(request)
        self.logger.info("→ %s %s stream=%s", method, path, stream)

        try:
            _, config_name, jwt_token = self._get_config_and_token()
        except Exception as e:
            self.logger.error("Auth failed: %s", e)
            self._send_error(503, "Auth error")
            clear_request_context()
            return

        with component_context("FILTER"):
            filter_rule = self.request_filter.find_matching_rule(request, path)
            if filter_rule:
                path = self.request_filter.filter_query_params(
                    path, filter_rule.query_params_to_remove
                )

        target_url = f"{self.request_forwarder.target_base_url}{path}"
        headers = self._build_headers(request, jwt_token, filter_rule)
        body = self._get_request_body(request, filter_rule)

        if self.logger.isEnabledFor(logging.DEBUG):
            self._log_request_details(method, path, target_url, headers, body)

        with component_context("FWD"):
            self.logger.info("Sending request to backend: %s", target_url)

        pipe_r, pipe_w = os.pipe()
        try:
            state = StreamingState(
                pipe_r=pipe_r,
                pipe_w=pipe_w,
                chunk_queue=queue.Queue(),
                cancel=threading.Event(),
                req_id=req_id,
                config_name=config_name,
                thread=None,
                start_time=start_time,
                stream=stream,
            )
            state.thread = threading.Thread(
                target=self._streaming_worker,
                args=(method, target_url, headers, body, state),
                name=f"streaming-{req_id}",
                daemon=True,
            )
            self._streaming_state = state
            state.thread.start()
        except Exception as setup_exc:
            self.logger.error("Failed to start streaming: %s", setup_exc, exc_info=True)
            try:
                os.close(pipe_r)
            except OSError:
                pass
            try:
                os.close(pipe_w)
            except OSError:
                pass
            self._streaming_state = None
            self._send_error(500, "Failed to start streaming")
            clear_request_context()
        # Return immediately — event loop resumes, read_from_descriptors delivers data

    def _build_headers(
        self,
        request: HttpParser,
        jwt_token: str,
        filter_rule: FilterRule | None = None,
    ) -> dict[str, str]:
        """Build headers for forwarding request.

        Args:
            request: HTTP request object
            jwt_token: JWT token for authentication
            filter_rule: Filter rule to apply, if any

        Returns:
            Headers dictionary for forwarding
        """
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Host": "flow.ciandt.com",
        }

        if not request.headers:
            return headers

        # Get headers to skip (including filtered headers)
        skip_headers = self.request_filter.get_headers_to_skip(filter_rule)

        for header_name, header_value in request.headers.items():
            name = self._decode_bytes(header_name).lower()
            if name not in skip_headers:
                headers[self._decode_bytes(header_name)] = self._extract_header_value(
                    header_value
                )

        return headers

    def _get_request_body(
        self, request: HttpParser, filter_rule: FilterRule | None = None
    ) -> bytes | None:
        """Extract and optionally filter request body.

        Args:
            request: HTTP request object
            filter_rule: Filter rule to apply, if any

        Returns:
            Request body bytes, filtered if rule provided
        """
        body = None
        if hasattr(request, "body") and request.body:
            body = request.body
        elif hasattr(request, "buffer") and request.buffer:
            body = bytes(request.buffer)

        # Apply filtering if rule provided
        if body and filter_rule:
            return self.request_filter.filter_body_params(
                body, filter_rule.body_params_to_remove
            )

        return body

    @staticmethod
    def _parse_stream_field(request: "HttpParser") -> "bool | None":
        """Parse the 'stream' field from the JSON request body.

        Returns True/False if the field is present and parseable, None otherwise.
        All indeterminate cases (absent body, non-JSON, missing field) return None.
        """
        body = None
        if hasattr(request, "body") and request.body:
            body = request.body
        elif hasattr(request, "buffer") and request.buffer:
            body = bytes(request.buffer)
        if not body:
            return None
        try:
            parsed = json.loads(body)
            return parsed.get("stream")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _log_request_details(  # pylint: disable=too-many-positional-arguments
        self,
        method: str,
        path: str,
        target_url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> None:
        """Log detailed request information in DEBUG mode."""
        self.logger.debug("  Method: %s", method)
        self.logger.debug("  Path: %s", path)
        self.logger.debug("  Target: %s", target_url)
        self.logger.debug("  Headers: %s", headers)

        if body:
            try:
                body_str = body.decode("utf-8", errors="replace")
                if len(body_str) > 2000:
                    self.logger.debug(
                        "  Body (%d bytes, truncated): %s...",
                        len(body),
                        body_str[:2000],
                    )
                else:
                    self.logger.debug("  Body (%d bytes): %s", len(body), body_str)
            except Exception as e:
                self.logger.debug("  Body: %d bytes (decode error: %s)", len(body), e)
        else:
            self.logger.debug("  Body: None")

    @staticmethod
    def _encode_sse_line(raw: str) -> bytes:
        """Encode a single SSE line to bytes.

        Empty string (event boundary separator) → b'\\n'.
        Content line → (raw + '\\n').encode().
        """
        if raw == "":
            return b"\n"
        return (raw + "\n").encode()

    def _send_response_headers_from(self, item: _ResponseHeaders) -> None:
        """Send HTTP status line and headers from a _ResponseHeaders item.

        Called from the main thread only (read_from_descriptors path).
        Accepts plain-data _ResponseHeaders — no httpx objects cross thread boundaries.
        """
        status_line = f"HTTP/1.1 {item.status_code} {item.reason_phrase}\r\n"
        self.client.queue(memoryview(status_line.encode()))

        # Always strip connection and transfer-encoding: we stream raw bytes to the
        # client, not chunked framing (hex size + CRLF per chunk). Keeping
        # transfer-encoding would cause clients to expect chunked format and
        # raise InvalidHTTPResponse when they see raw SSE/body bytes.
        skip_headers = {"connection", "transfer-encoding"}

        for name, value in item.headers.items():
            if name.lower() not in skip_headers:
                self.client.queue(memoryview(f"{name}: {value}\r\n".encode()))

        if item.is_sse:
            self.client.queue(memoryview(b"Cache-Control: no-cache\r\n"))
            self.client.queue(memoryview(b"X-Accel-Buffering: no\r\n"))

        self.client.queue(memoryview(b"\r\n"))

    def _streaming_worker(  # pylint: disable=too-many-positional-arguments
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        state: StreamingState,
    ) -> None:
        """Background thread: opens httpx stream, feeds chunks into state.chunk_queue.

        Queue protocol (strict order):
          1. _ResponseHeaders  — response metadata for main thread to send headers
          2. bytes             — one item per non-empty chunk/encoded SSE line
          3. None              — sentinel, always last (even on error)

        Never touches self.client. Thread-safety invariant: only main thread uses self.client.
        """
        set_request_context(state.req_id, "WS")
        try:
            http_client = ProcessServices.get().get_http_client()
            with http_client.stream(
                method=method,
                url=url,
                headers=headers,
                content=body,
                timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
                follow_redirects=True,
            ) as response:
                is_sse = "text/event-stream" in response.headers.get("content-type", "")
                state.chunk_queue.put(
                    _ResponseHeaders(
                        status_code=response.status_code,
                        reason_phrase=response.reason_phrase,
                        headers=dict(response.headers),
                        is_sse=is_sse,
                    )
                )
                try:
                    os.write(state.pipe_w, b"\x00")
                except OSError:
                    return  # client already disconnected

                if is_sse:
                    for line in response.iter_lines():
                        if state.cancel.is_set():
                            break
                        chunk = self._encode_sse_line(line)
                        if chunk:
                            if state.ttfb is None:
                                state.ttfb = time.time() - state.start_time
                                transfer = response.headers.get("transfer-encoding", "none")
                                self.logger.info(
                                    "backend=%d transfer=%s ttfb=%.1fs",
                                    response.status_code,
                                    transfer,
                                    state.ttfb,
                                )
                            state.chunk_queue.put(chunk)
                            try:
                                os.write(state.pipe_w, b"\x00")
                            except OSError:
                                return
                else:
                    for chunk in response.iter_bytes():
                        if state.cancel.is_set():
                            break
                        if not chunk:
                            continue
                        if state.ttfb is None:
                            state.ttfb = time.time() - state.start_time
                            transfer = response.headers.get("transfer-encoding", "none")
                            self.logger.info(
                                "backend=%d transfer=%s ttfb=%.1fs",
                                response.status_code,
                                transfer,
                                state.ttfb,
                            )
                        state.chunk_queue.put(chunk)
                        try:
                            os.write(state.pipe_w, b"\x00")
                        except OSError:
                            return

        except httpx.TransportError as e:
            self.logger.warning("Transport error — marking httpx client dirty: %s", e)
            ProcessServices.get().mark_http_client_dirty()
            state.error = e
        except Exception as e:
            self.logger.warning("Worker error: %s", e, exc_info=True)
            state.error = e
        finally:
            state.chunk_queue.put(None)
            try:
                os.write(state.pipe_w, b"\x00")
            except OSError:
                pass  # pipe may already be closed (client disconnected)

    _REASON_PHRASES: dict[int, str] = {
        500: "Internal Server Error",
        503: "Service Unavailable",
    }

    def _send_sse_error_event(self) -> None:
        """Inject a synthetic SSE error event to notify the client of upstream failure.

        Called from _finish_stream (main thread only).
        Thread-safety: self.client.queue() is only called from the main thread.
        """
        payload = json.dumps(
            {
                "type": "error",
                "error": {"type": "api_error", "message": "Upstream connection lost"},
            }
        )
        event = f"event: error\ndata: {payload}\n\n".encode()
        self.client.queue(memoryview(event))

    def _send_error(
        self, status_code: int = 500, message: str = "Internal server error"
    ) -> None:
        """Send error response to client."""
        reason = self._REASON_PHRASES.get(status_code, "Error")
        error_response = (
            f"HTTP/1.1 {status_code} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f'{{"error": "{message}"}}'
        )
        self.client.queue(memoryview(error_response.encode()))
