"""Main FlowProxyPlugin class implementation."""

import os
import secrets
import threading
from typing import Any, Optional

from proxy.http.parser import HttpParser
from proxy.http.proxy import HttpProxyBasePlugin

from ..utils.log_context import clear_request_context, set_request_context
from ..utils.plugin_pool import PluginPool
from .base_plugin import BaseFlowProxyPlugin

_proxy_pool: Optional["PluginPool[FlowProxyPlugin]"] = None
_proxy_pool_lock = threading.Lock()
_PROXY_POOL_SIZE = int(os.getenv("FLOW_PROXY_PLUGIN_POOL_SIZE", "64"))


class FlowProxyPlugin(HttpProxyBasePlugin, BaseFlowProxyPlugin):
    """Flow LLM Proxy authentication plugin for forward proxy mode.

    This plugin handles authentication token generation and request forwarding
    to Flow LLM Proxy service with round-robin load balancing when used with
    proxy mode (curl -x).
    """

    _log_once: bool = False  # Class variable: log initialization message only once

    def __new__(  # pylint: disable=too-many-positional-arguments
        cls,
        uid: str,
        flags: Any,
        client: Any,
        event_queue: Any,
        upstream_conn_pool: Any = None,
    ) -> "FlowProxyPlugin":
        global _proxy_pool  # pylint: disable=global-statement
        if _proxy_pool is None:
            with _proxy_pool_lock:
                if _proxy_pool is None:
                    _proxy_pool = PluginPool(cls, max_size=_PROXY_POOL_SIZE)
        return _proxy_pool.acquire(uid, flags, client, event_queue, upstream_conn_pool)

    def __init__(  # pylint: disable=too-many-positional-arguments
        self,
        uid: str,
        flags: Any,
        client: Any,
        event_queue: Any,
        upstream_conn_pool: Any = None,
    ) -> None:
        """Initialize plugin and load authentication configurations."""
        if self._pooled:
            return  # Reuse: _rebind() already ran in pool.acquire()
        # First-time initialization (runs exactly once per instance)
        HttpProxyBasePlugin.__init__(
            self, uid, flags, client, event_queue, upstream_conn_pool
        )
        self._init_services()
        self._pooled = True
        if not FlowProxyPlugin._log_once:
            FlowProxyPlugin._log_once = True
            self.logger.info("FlowProxyPlugin initialized (pooled)")

    def _rebind(  # pylint: disable=too-many-positional-arguments,arguments-differ
        self,
        uid: str,
        flags: Any,
        client: Any,
        event_queue: Any,
        upstream_conn_pool: Any = None,
    ) -> None:
        """Rebind proxy.py connection-specific state for pool reuse."""
        HttpProxyBasePlugin.__init__(
            self, uid, flags, client, event_queue, upstream_conn_pool
        )
        self._pooled = True  # Ensure flag stays True

    def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
        """Process request before establishing upstream connection.

        Implements the core request interception logic:
        1. Converts reverse proxy requests to forward proxy format
        2. Validates the incoming request
        3. Selects authentication configuration with round-robin load balancing
        4. Generates JWT token with failover support
        5. Modifies request headers to include authentication
        6. Redirects request to Flow LLM Proxy endpoint

        Args:
            request: The incoming HTTP request from the client

        Returns:
            Modified request with authentication headers, or None to reject
        """
        req_id = secrets.token_hex(3)
        set_request_context(req_id, "PROXY")
        try:
            # Convert reverse proxy requests to forward proxy format
            self._convert_reverse_proxy_request(request)

            # Validate request
            if not self.request_forwarder.validate_request(request):
                self.logger.error("Request validation failed")
                return None

            # Get config and token with failover
            _, config_name, jwt_token = self._get_config_and_token()

            # Modify request headers
            modified_request = self.request_forwarder.modify_request_headers(
                request, jwt_token, config_name
            )

            # Log success
            method = self._decode_bytes(request.method) if request.method else "GET"
            path = self._decode_bytes(request.path) if request.path else "unknown"
            self.logger.info("→ %s %s [%s]", method, path, config_name)

            return modified_request

        except (RuntimeError, ValueError) as e:
            self.logger.error("Request processing failed: %s", str(e))
            return None
        except Exception as e:
            self.logger.error("Unexpected error: %s", str(e), exc_info=True)
            return None
        finally:
            clear_request_context()

    def _convert_reverse_proxy_request(self, request: HttpParser) -> None:
        """Convert reverse proxy request (path only) to forward proxy format (full URL).

        Args:
            request: HTTP request to potentially convert
        """
        if not request.path:
            return

        # Check if it's already a full URL
        if request.path.startswith(b"http://") or request.path.startswith(b"https://"):
            return

        # Convert path-only request to full URL
        original_path = self._decode_bytes(request.path)
        target_url = f"{self.request_forwarder.target_base_url}{original_path}"
        request.set_url(target_url.encode())

        self.logger.debug(
            "Converted reverse proxy request: %s → %s", original_path, target_url
        )

    def handle_client_request(self, request: HttpParser) -> HttpParser | None:
        """Handle client request and add authentication information.

        This method delegates to before_upstream_connection for processing.

        Args:
            request: The client HTTP request

        Returns:
            Processed request with authentication or None to reject
        """
        return self.before_upstream_connection(request)

    def handle_upstream_chunk(self, chunk: memoryview) -> memoryview | None:
        """Handle upstream response data with transparent pass-through.

        Supports both regular and streaming responses by forwarding chunks
        without modification or buffering.

        Args:
            chunk: Response data chunk from upstream server

        Returns:
            Unmodified chunk for transparent pass-through
        """
        try:
            if chunk:
                self.logger.debug("Received upstream chunk: %d bytes", len(chunk))
                return self.request_forwarder.handle_response_chunk(chunk)

            self.logger.debug("Received empty chunk from upstream")
            return chunk

        except Exception as e:
            self.logger.error(
                "Error handling upstream chunk: %s", str(e), exc_info=True
            )
            return chunk  # Return chunk anyway to maintain connection stability

    def on_upstream_connection_close(self) -> None:
        """Return instance to pool on upstream connection close."""
        self.logger.info("Upstream connection closed")
        if _proxy_pool is not None and self._pooled:
            _proxy_pool.release(self)

    def on_access_log(self, context: dict[str, Any]) -> dict[str, Any] | None:
        """Add plugin-specific information to access log.

        Args:
            context: Access log context dictionary

        Returns:
            Modified context with plugin information
        """
        try:
            context.update(
                {
                    "plugin": "FlowProxyPlugin",
                    "load_balancer_stats": {
                        "available_configs": self.load_balancer.available_count,
                        "failed_configs": self.load_balancer.failed_count,
                        "total_requests": self.load_balancer.total_requests,
                    },
                }
            )

            self.logger.debug("Access log: %s", context)
            return context

        except Exception as e:
            self.logger.error("Error in access log handler: %s", str(e), exc_info=True)
            return context
