"""Web Server plugin for reverse proxy mode."""

import logging
from typing import Any

import requests
from proxy.http.parser import HttpParser
from proxy.http.server import HttpWebServerBasePlugin, httpProtocolTypes

from .base_plugin import BaseFlowProxyPlugin
from .request_filter import FilterRule, RequestFilter


class FlowProxyWebServerPlugin(HttpWebServerBasePlugin, BaseFlowProxyPlugin):
    """Flow LLM Proxy web server plugin for reverse proxy mode.

    This plugin handles direct HTTP requests (reverse proxy mode) and forwards
    them to Flow LLM Proxy with authentication.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize web server plugin."""
        super().__init__(*args, **kwargs)
        self._setup_logging()
        self.logger.info("Initializing FlowProxyWebServerPlugin...")
        self._initialize_components()
        self.request_filter = RequestFilter(self.logger)

    def routes(self) -> list[tuple[int, str]]:
        """Define routes that this plugin handles."""
        return [(httpProtocolTypes.HTTP, r"/.*")]

    def handle_request(self, request: HttpParser) -> None:
        """Handle web server request."""
        method = self._decode_bytes(request.method) if request.method else "GET"
        path = self._decode_bytes(request.path) if request.path else "/"

        self.logger.info("→ %s %s", method, path)

        try:
            # Get config and token
            _, config_name, jwt_token = self._get_config_and_token()

            # Forward request
            response = self._forward_request(request, method, path, jwt_token)

            # Send response
            self._send_response(response)

            # Log result
            log_func = (
                self.logger.info if response.status_code < 400 else self.logger.warning
            )
            log_func("← %d %s [%s]", response.status_code, response.reason, config_name)

        except Exception as e:
            self.logger.error("✗ Request failed: %s", str(e), exc_info=True)
            self._send_error()

    def _forward_request(
        self, request: HttpParser, method: str, path: str, jwt_token: str
    ) -> requests.Response:
        """Forward request to upstream server with filtering.

        Args:
            request: Original HTTP request
            method: HTTP method
            path: Request path
            jwt_token: JWT token for authentication

        Returns:
            Response from upstream server
        """
        # Find matching filter rule
        filter_rule = self.request_filter.find_matching_rule(request, path)

        # Apply filtering if rule matches
        if filter_rule:
            path = self.request_filter.filter_query_params(
                path, filter_rule.query_params_to_remove
            )

        target_url = f"{self.request_forwarder.target_base_url}{path}"
        headers = self._build_headers(request, jwt_token, filter_rule)
        body = self._get_request_body(request, filter_rule)

        if self.logger.isEnabledFor(logging.DEBUG):
            self._log_request_details(method, path, target_url, headers, body)

        # Log the final URL being sent to backend
        self.logger.info("Sending request to backend: %s", target_url)

        return requests.request(
            method=method,
            url=target_url,
            headers=headers,
            data=body,
            stream=True,
            timeout=(30, 600),  # 30s connect, 600s read for streaming
        )

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

    def _send_response(self, response: requests.Response) -> None:
        """Send response back to client with graceful error handling."""
        if self.logger.isEnabledFor(logging.DEBUG):
            self._log_response_details(response)

        bytes_sent = 0
        chunks_sent = 0

        try:
            # Send status line and headers
            self._send_response_headers(response)

            # Stream response body
            bytes_sent, chunks_sent = self._stream_response_body(response)

            if chunks_sent > 0:
                self.logger.debug(
                    "Streaming completed: %d bytes in %d chunks",
                    bytes_sent,
                    chunks_sent,
                )

        except (BrokenPipeError, ConnectionResetError) as e:
            self.logger.debug(
                "Client disconnected (%s) - sent %d bytes", type(e).__name__, bytes_sent
            )
        except Exception as e:
            self.logger.error(
                "Unexpected error streaming response: %s (sent %d bytes)",
                e,
                bytes_sent,
                exc_info=True,
            )
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _log_response_details(self, response: requests.Response) -> None:
        """Log detailed response information in DEBUG mode."""
        self.logger.debug("Response: %d %s", response.status_code, response.reason)
        self.logger.debug("  Response Headers: %s", dict(response.headers))

        # Try to peek at response body without consuming it
        try:
            # For streaming responses, we can't read the body without consuming it
            # So we just log that it's a streaming response
            if (
                response.headers.get("Transfer-Encoding") == "chunked"
                or "stream" in response.headers.get("Content-Type", "").lower()
            ):
                self.logger.debug("  Response Body: <streaming response>")
            else:
                # For non-streaming, we could read but need to be careful
                # Since we're streaming anyway, just log the content type
                content_type = response.headers.get("Content-Type", "unknown")
                content_length = response.headers.get("Content-Length", "unknown")
                self.logger.debug(
                    "  Response Body: Content-Type=%s, Content-Length=%s",
                    content_type,
                    content_length,
                )
        except Exception as e:
            self.logger.debug("  Response Body: <error reading: %s>", e)

    def _send_response_headers(self, response: requests.Response) -> None:
        """Send HTTP status line and headers."""
        # Status line
        status_line = f"HTTP/1.1 {response.status_code} {response.reason}\r\n"
        self.client.queue(memoryview(status_line.encode()))

        # Headers (skip connection and transfer-encoding)
        for name, value in response.headers.items():
            if name.lower() not in {"connection", "transfer-encoding"}:
                self.client.queue(memoryview(f"{name}: {value}\r\n".encode()))

        # End of headers
        self.client.queue(memoryview(b"\r\n"))

    def _stream_response_body(self, response: requests.Response) -> tuple[int, int]:
        """Stream response body to client.

        Returns:
            Tuple of (bytes_sent, chunks_sent)
        """
        bytes_sent = 0
        chunks_sent = 0

        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue

            # Check client connection
            if not self._is_client_connected():
                self.logger.debug(
                    "Client disconnected - stopping (sent %d bytes in %d chunks)",
                    bytes_sent,
                    chunks_sent,
                )
                break

            try:
                self.client.queue(memoryview(chunk))
                bytes_sent += len(chunk)
                chunks_sent += 1

                if hasattr(self.client, "flush"):
                    self.client.flush()

            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                if isinstance(e, OSError) and e.errno != 32:  # Not broken pipe
                    self.logger.warning("OS error during streaming: %s", e)
                else:
                    self.logger.debug(
                        "Client disconnected during streaming - sent %d bytes",
                        bytes_sent,
                    )
                break

        return bytes_sent, chunks_sent

    def _is_client_connected(self) -> bool:
        """Check if client connection is still active."""
        try:
            return (
                hasattr(self.client, "connection")
                and self.client.connection is not None
            )
        except Exception:
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
