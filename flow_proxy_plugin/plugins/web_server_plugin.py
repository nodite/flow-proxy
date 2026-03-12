"""Web Server plugin for reverse proxy mode."""

import logging
import os
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
class StreamStats:
    """Metrics collected during a single streaming response."""

    start_time: float
    first_chunk_time: float | None = None
    end_time: float | None = None
    bytes_sent: int = 0
    chunks_sent: int = 0  # non-empty payload writes only
    event_count: int = 0  # SSE only: incremented at each empty-line boundary
    completed: bool = False  # True only if stream finished without interruption

    @property
    def ttft_ms(self) -> float | None:
        """Time-to-first-token in milliseconds, or None if no data received."""
        if self.first_chunk_time is None:
            return None
        return (self.first_chunk_time - self.start_time) * 1000

    @property
    def duration_ms(self) -> float | None:
        """Stream duration (first chunk -> last chunk) in ms, or None if incomplete."""
        if self.end_time is None or self.first_chunk_time is None:
            return None
        return (self.end_time - self.first_chunk_time) * 1000


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

    def on_client_connection_close(self) -> None:
        """Return instance to pool when connection closes."""
        if _web_pool is not None and self._pooled:
            _web_pool.release(self)

    def routes(self) -> list[tuple[int, str]]:
        """Define routes that this plugin handles."""
        return [(httpProtocolTypes.HTTP, r"/.*")]

    def handle_request(self, request: HttpParser) -> None:
        """Handle web server request."""
        method = self._decode_bytes(request.method) if request.method else "GET"
        path = self._decode_bytes(request.path) if request.path else "/"

        req_id = secrets.token_hex(3)
        set_request_context(req_id, "WS")
        try:
            self.logger.info("→ %s %s", method, path)

            try:
                _, config_name, jwt_token = self._get_config_and_token()

                # Build request params
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

                http_client = ProcessServices.get().get_http_client()
                with http_client.stream(
                    method=method,
                    url=target_url,
                    headers=headers,
                    content=body,
                    timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
                ) as response:
                    self.logger.info(
                        "Backend response: %d %s, Transfer-Encoding: %s, Content-Length: %s",
                        response.status_code,
                        response.reason_phrase,
                        response.headers.get("transfer-encoding", "none"),
                        response.headers.get("content-length", "none"),
                    )

                    if self.logger.isEnabledFor(logging.DEBUG):
                        self._log_response_details(response)

                    self._send_response_headers(response)
                    self._stream_response_body(response)

                    log_func = (
                        self.logger.info
                        if response.status_code < 400
                        else self.logger.warning
                    )
                    log_func(
                        "← %d %s [%s]",
                        response.status_code,
                        response.reason_phrase,
                        config_name,
                    )

            except (BrokenPipeError, ConnectionResetError) as e:
                self.logger.debug("Client disconnected (%s)", type(e).__name__)
            except httpx.RemoteProtocolError as e:
                self.logger.error(
                    "Backend streaming failed (RemoteProtocolError): %s",
                    str(e),
                    exc_info=True,
                )
            except httpx.TransportError as e:
                self.logger.error("Transport error — marking httpx client dirty: %s", e)
                ProcessServices.get().mark_http_client_dirty()
                self._send_error(503, "Upstream transport error")
            except Exception as e:
                self.logger.error("✗ Request failed: %s", str(e), exc_info=True)
                self._send_error()
        finally:
            clear_request_context()

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

    def _prepare_headers(self, request: HttpParser, jwt_token: str) -> dict[str, str]:
        """Prepare headers for forwarding request.

        Deprecated: Use _build_headers instead. Kept for backward compatibility.
        """
        return self._build_headers(request, jwt_token)

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

    def _log_response_details(self, response: httpx.Response) -> None:
        """Log detailed response information in DEBUG mode."""
        self.logger.debug(
            "Response: %d %s", response.status_code, response.reason_phrase
        )
        self.logger.debug("  Response Headers: %s", dict(response.headers))

        content_type = response.headers.get("content-type", "unknown")
        if "text/event-stream" in content_type:
            self.logger.debug("  Response Body: <SSE streaming response>")
        else:
            content_length = response.headers.get("content-length", "unknown")
            self.logger.debug(
                "  Response Body: Content-Type=%s, Content-Length=%s",
                content_type,
                content_length,
            )

    def _send_response_headers(self, response: httpx.Response) -> None:
        """Send HTTP status line and headers."""
        is_sse = "text/event-stream" in response.headers.get("content-type", "")

        # Status line
        status_line = f"HTTP/1.1 {response.status_code} {response.reason_phrase}\r\n"
        self.client.queue(memoryview(status_line.encode()))

        # Headers
        skip_headers = {"connection"}
        if not is_sse:
            skip_headers.add("transfer-encoding")

        for name, value in response.headers.items():
            if name.lower() not in skip_headers:
                self.client.queue(memoryview(f"{name}: {value}\r\n".encode()))

        # SSE-specific headers to prevent intermediate proxy buffering
        if is_sse:
            self.client.queue(memoryview(b"Cache-Control: no-cache\r\n"))
            self.client.queue(memoryview(b"X-Accel-Buffering: no\r\n"))

        # End of headers
        self.client.queue(memoryview(b"\r\n"))

    def _stream_response_body(self, response: httpx.Response) -> StreamStats:
        """Stream response body to client; returns stats for the completed stream.

        stats.completed is set inside _stream_bytes/_stream_sse only when the
        iterator exhausts cleanly. The dispatcher does not set it — this lets
        disconnect-interrupted streams correctly have completed=False.
        """
        is_sse = "text/event-stream" in response.headers.get("content-type", "")
        stats = StreamStats(start_time=time.perf_counter())
        try:
            if is_sse:
                self._stream_sse(response, stats)  # type: ignore[attr-defined]  # added in Task 7
            else:
                self._stream_bytes(response, stats)
        finally:
            stats.end_time = time.perf_counter()
            self._log_stream_stats(stats, is_sse)
        return stats

    def _stream_bytes(self, response: httpx.Response, stats: StreamStats) -> None:
        """Stream non-SSE response body to client, updating stats in place.

        Sets stats.completed = True only if the iterator is exhausted without
        interruption. Returns early (completed stays False) on disconnect.
        """
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            if stats.first_chunk_time is None:
                stats.first_chunk_time = time.perf_counter()
                self.logger.info(
                    "Received first chunk from backend: %d bytes", len(chunk)
                )
            try:
                self.client.queue(memoryview(chunk))
                stats.bytes_sent += len(chunk)
                stats.chunks_sent += 1
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                if isinstance(e, OSError) and e.errno != 32:
                    self.logger.warning("OS error during streaming: %s", e)
                else:
                    self.logger.debug(
                        "Client disconnected during streaming - sent %d bytes",
                        stats.bytes_sent,
                    )
                return  # completed stays False
        stats.completed = True  # loop exhausted cleanly

    def _log_stream_stats(self, stats: StreamStats, is_sse: bool) -> None:
        """Log a single summary line after streaming completes or is interrupted."""
        if is_sse:
            if stats.completed:
                if stats.ttft_ms is not None:
                    self.logger.info(
                        "SSE stream complete: TTFT=%.0fms, duration=%.0fms, events=%d, bytes=%d",
                        stats.ttft_ms,
                        stats.duration_ms or 0.0,
                        stats.event_count,
                        stats.bytes_sent,
                    )
                else:
                    self.logger.info(
                        "SSE stream complete: no data received, bytes=%d", stats.bytes_sent
                    )
            else:
                if stats.ttft_ms is not None:
                    self.logger.info(
                        "SSE stream interrupted: TTFT=%.0fms, events=%d, bytes=%d",
                        stats.ttft_ms,
                        stats.event_count,
                        stats.bytes_sent,
                    )
                else:
                    self.logger.info(
                        "SSE stream interrupted: no data received, bytes=%d", stats.bytes_sent
                    )
        else:
            prefix = "Stream complete" if stats.completed else "Stream interrupted"
            self.logger.info(
                "%s: bytes=%d, chunks=%d", prefix, stats.bytes_sent, stats.chunks_sent
            )

    def _is_client_connected(self) -> bool:
        """Check if client connection is still active."""
        try:
            has_connection_attr = hasattr(self.client, "connection")
            if not has_connection_attr:
                self.logger.debug("Client has no 'connection' attribute")
                return False

            connection_not_none = self.client.connection is not None
            self.logger.debug(
                "Client connection check: has_attr=%s, not_none=%s",
                has_connection_attr,
                connection_not_none,
            )
            return connection_not_none
        except Exception as e:
            self.logger.debug("Client connection check exception: %s", e)
            return False

    def _send_error(
        self, status_code: int = 500, message: str = "Internal server error"
    ) -> None:
        """Send error response to client."""
        error_response = (
            f"HTTP/1.1 {status_code} Error\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f'{{"error": "{message}"}}'
        )
        self.client.queue(memoryview(error_response.encode()))
