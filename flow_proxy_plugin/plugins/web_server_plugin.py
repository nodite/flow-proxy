"""Web Server plugin for reverse proxy mode."""

import logging
from typing import Any

import requests
from proxy.http.parser import HttpParser
from proxy.http.server import HttpWebServerBasePlugin, httpProtocolTypes

from .base_plugin import BaseFlowProxyPlugin


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
        """Forward request to upstream server.

        Args:
            request: Original HTTP request
            method: HTTP method
            path: Request path
            jwt_token: JWT token for authentication

        Returns:
            Response from upstream server
        """
        target_url = f"{self.request_forwarder.target_base_url}{path}"
        headers = self._build_headers(request, jwt_token)
        body = self._get_request_body(request)

        if self.logger.isEnabledFor(logging.DEBUG):
            self._log_request_details(method, path, target_url, headers, body)

        return requests.request(
            method=method,
            url=target_url,
            headers=headers,
            data=body,
            stream=True,
            timeout=(30, 600),  # 30s connect, 600s read for streaming
        )

    def _build_headers(self, request: HttpParser, jwt_token: str) -> dict[str, str]:
        """Build headers for forwarding request."""
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Host": "flow.ciandt.com",
        }

        if not request.headers:
            return headers

        # Copy headers except those we're overriding
        skip_headers = {"host", "connection", "content-length", "authorization"}

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

    def _get_request_body(self, request: HttpParser) -> bytes | None:
        """Extract request body from request."""
        if hasattr(request, "body") and request.body:
            return request.body
        if hasattr(request, "buffer") and request.buffer:
            return bytes(request.buffer)
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

    def _send_response(self, response: requests.Response) -> None:
        """Send response back to client with graceful error handling."""
        self.logger.debug("Response: %d %s", response.status_code, response.reason)

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
