"""Web Server plugin for reverse proxy mode."""

import logging
import os
from typing import Any

import requests
from proxy.http.parser import HttpParser
from proxy.http.server import HttpWebServerBasePlugin, httpProtocolTypes

from ..utils.logging import setup_colored_logger
from ..utils.plugin_base import initialize_plugin_components


class FlowProxyWebServerPlugin(HttpWebServerBasePlugin):
    """Flow LLM Proxy web server plugin for reverse proxy mode.

    This plugin handles direct HTTP requests (reverse proxy mode) and forwards
    them to Flow LLM Proxy with authentication.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize web server plugin."""
        super().__init__(*args, **kwargs)

        self.logger = logging.getLogger(__name__)

        # Set log level from environment variable (set by CLI) or flags
        log_level_str = os.getenv("FLOW_PROXY_LOG_LEVEL", "INFO")

        if hasattr(self, "flags") and hasattr(self.flags, "log_level"):
            flags_level = getattr(self.flags, "log_level", None)
            # Only use flags if env var is not set
            if not os.getenv("FLOW_PROXY_LOG_LEVEL") and isinstance(flags_level, str):
                log_level_str = flags_level

        if isinstance(log_level_str, str):
            setup_colored_logger(self.logger, log_level_str)

        self.logger.info("Initializing FlowProxyWebServerPlugin...")

        try:
            # Initialize components
            (
                self.secrets_manager,
                self.configs,
                self.load_balancer,
                self.jwt_generator,
                self.request_forwarder,
            ) = initialize_plugin_components(self.logger)

            self.logger.info(
                "✓ Web server plugin ready (%d configs)", len(self.configs)
            )
        except Exception as e:
            self.logger.critical(f"Failed to initialize: {str(e)}")
            raise

    def routes(self) -> list[tuple[int, str]]:
        """Define routes that this plugin handles."""
        return [
            (httpProtocolTypes.HTTP, r"/.*"),  # Match all paths
        ]

    def _prepare_headers(self, request: HttpParser, jwt_token: str) -> dict[str, str]:
        """Prepare headers for forwarding request.

        Args:
            request: The HTTP request
            jwt_token: JWT token for authentication

        Returns:
            Dictionary of headers
        """
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Host": "flow.ciandt.com",
        }

        if request.headers:
            for header_name, header_value in request.headers.items():
                # Decode header name
                name = (
                    header_name.decode()
                    if isinstance(header_name, bytes)
                    else header_name
                )

                # Skip headers we're overriding
                if name.lower() in [
                    "host",
                    "connection",
                    "content-length",
                    "authorization",
                ]:
                    continue

                # Extract actual value from tuple (header_value is a tuple like (value, b''))
                if isinstance(header_value, tuple):
                    actual_value = header_value[0]
                else:
                    actual_value = header_value

                # Decode value
                value = (
                    actual_value.decode()
                    if isinstance(actual_value, bytes)
                    else str(actual_value)
                )

                headers[name] = value

        return headers

    def _is_client_connected(self) -> bool:
        """Check if client connection is still active.

        Returns:
            True if client appears to be connected, False otherwise
        """
        try:
            return (
                hasattr(self.client, "connection")
                and self.client.connection is not None
            )
        except Exception:
            return False

    def _send_response(self, response: requests.Response) -> None:
        """Send response back to client with graceful error handling.

        Args:
            response: Response from upstream server
        """
        # Log response details
        self.logger.debug("Response status: %d", response.status_code)
        self.logger.debug("Response headers: %s", dict(response.headers))

        bytes_sent = 0
        chunks_sent = 0

        try:
            # Send status line
            response_line = f"HTTP/1.1 {response.status_code} {response.reason}\r\n"
            self.client.queue(memoryview(response_line.encode()))

            # Send headers
            for header_name, header_value in response.headers.items():
                if header_name.lower() not in ["connection", "transfer-encoding"]:
                    self.client.queue(
                        memoryview(f"{header_name}: {header_value}\r\n".encode())
                    )

            self.client.queue(memoryview(b"\r\n"))

            # Stream the content without buffering
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue

                # Check if client is still connected before sending
                if not self._is_client_connected():
                    self.logger.debug(
                        "Client disconnected, stopping response streaming "
                        "(sent %d bytes in %d chunks)",
                        bytes_sent,
                        chunks_sent,
                    )
                    break

                try:
                    self.client.queue(memoryview(chunk))
                    bytes_sent += len(chunk)
                    chunks_sent += 1

                    # Flush immediately for streaming responses
                    if hasattr(self.client, "flush"):
                        self.client.flush()

                except (BrokenPipeError, ConnectionResetError) as e:
                    # Client disconnected - this is normal for streaming responses
                    # Especially common when client has received all needed data
                    self.logger.debug(
                        "Client disconnected during streaming (%s) - sent %d bytes in %d chunks",
                        type(e).__name__,
                        bytes_sent,
                        chunks_sent,
                    )
                    break
                except OSError as e:
                    # Other OS-level errors (e.g., EPIPE)
                    if e.errno == 32:  # Broken pipe
                        self.logger.debug(
                            "Broken pipe during streaming - sent %d bytes in %d chunks",
                            bytes_sent,
                            chunks_sent,
                        )
                    else:
                        self.logger.warning(
                            "OS error during streaming: %s - sent %d bytes",
                            e,
                            bytes_sent,
                        )
                    break

            # Log successful completion
            if chunks_sent > 0:
                self.logger.debug(
                    "Response streaming completed: %d bytes in %d chunks",
                    bytes_sent,
                    chunks_sent,
                )

        except (BrokenPipeError, ConnectionResetError) as e:
            # Connection lost during header sending
            self.logger.debug(
                "Client disconnected during response headers (%s)", type(e).__name__
            )
        except Exception as e:
            # Unexpected errors should still be logged as errors
            self.logger.error(
                "Unexpected error streaming response: %s (sent %d bytes)",
                e,
                bytes_sent,
                exc_info=True,
            )
        finally:
            # Cleanup response resources
            try:
                response.close()
            except Exception:
                pass  # Ignore cleanup errors

    def _send_error(
        self, status_code: int = 500, message: str = "Internal server error"
    ) -> None:
        """Send error response to client.

        Args:
            status_code: HTTP status code
            message: Error message
        """
        error_response = (
            f"HTTP/1.1 {status_code} Error\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f'{{"error": "{message}"}}'
        )
        self.client.queue(memoryview(error_response.encode()))

    def handle_request(self, request: HttpParser) -> None:
        """Handle web server request."""
        method = request.method.decode() if request.method else "GET"
        path = request.path.decode() if request.path else "/"

        self.logger.info("→ %s %s", method, path)

        try:
            # Get config and generate token
            config = self.load_balancer.get_next_config()
            config_name = config.get("name", config.get("clientId", "unknown"))
            jwt_token = self.jwt_generator.generate_token(config)

            # Build target URL
            original_path = request.path.decode() if request.path else "/"
            target_url = f"{self.request_forwarder.target_base_url}{original_path}"

            # Prepare request
            headers = self._prepare_headers(request, jwt_token)

            # Get request body - try multiple ways
            body = None
            if hasattr(request, "body"):
                body = request.body
            elif hasattr(request, "buffer") and request.buffer is not None:
                body = bytes(request.buffer)

            # In DEBUG mode, log request details
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug("  Method: %s", method)
                self.logger.debug("  Path: %s", original_path)
                self.logger.debug("  Target: %s", target_url)
                self.logger.debug("  Config: %s", config_name)
                self.logger.debug("  Headers: %s", headers)

                if body:
                    try:
                        body_str = (
                            body.decode("utf-8", errors="replace")
                            if isinstance(body, bytes)
                            else str(body)
                        )
                        # Truncate if too long
                        if len(body_str) > 2000:
                            self.logger.debug(
                                "  Request body (%d bytes, truncated): %s...",
                                len(body),
                                body_str[:2000],
                            )
                        else:
                            self.logger.debug(
                                "  Request body (%d bytes): %s", len(body), body_str
                            )
                    except Exception as e:
                        self.logger.debug(
                            "  Request body: %d bytes (could not decode: %s)",
                            len(body),
                            e,
                        )
                else:
                    self.logger.debug("  Request body: None")

            # Forward request with appropriate timeout settings
            # (connect_timeout, read_timeout) - longer read timeout for streaming
            response = requests.request(
                method=method,
                url=target_url,
                headers=headers,
                data=body,
                stream=True,
                timeout=(
                    30,
                    600,
                ),  # 30s connect, 600s read for long streaming responses
            )

            # Send response
            self._send_response(response)

            # Log result with color indicator
            if response.status_code < 400:
                self.logger.info(
                    "← %d %s [%s]", response.status_code, response.reason, config_name
                )
            else:
                self.logger.warning(
                    "← %d %s [%s]", response.status_code, response.reason, config_name
                )

        except Exception as e:
            self.logger.error("✗ Request failed: %s", str(e), exc_info=True)
            self._send_error()
