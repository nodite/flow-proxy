"""Main FlowProxyPlugin class implementation."""

import logging
import os
from typing import Any

from proxy.http.parser import HttpParser
from proxy.http.proxy import HttpProxyBasePlugin

from ..utils.logging import setup_colored_logger
from ..utils.plugin_base import initialize_plugin_components


class FlowProxyPlugin(HttpProxyBasePlugin):
    """Flow LLM Proxy authentication plugin for forward proxy mode.

    This plugin handles authentication token generation and request forwarding
    to Flow LLM Proxy service with round-robin load balancing when used with
    proxy mode (curl -x).

    Inherits from HttpProxyBasePlugin to integrate with proxy.py framework.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize plugin, load authentication configurations.

        Loads authentication configurations from secrets.json and initializes
        all required components: SecretsManager, LoadBalancer, JWTGenerator,
        and RequestForwarder.

        Raises:
            FileNotFoundError: If secrets.json file is not found
            ValueError: If configuration is invalid or empty
        """
        super().__init__(*args, **kwargs)

        # Set up logging
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

        self.logger.info("Initializing FlowProxyPlugin...")

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
                f"FlowProxyPlugin successfully initialized with {len(self.configs)} authentication configurations"
            )

        except (FileNotFoundError, ValueError) as e:
            self.logger.critical(f"Failed to initialize FlowProxyPlugin: {str(e)}")
            raise
        except Exception as e:
            self.logger.critical(
                f"Unexpected error during FlowProxyPlugin initialization: {str(e)}"
            )
            raise

    def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
        """Process request before establishing upstream connection.

        This method is called by proxy.py before establishing a connection to the
        upstream server. It implements the core request interception logic:
        1. Validates the incoming request
        2. Converts reverse proxy requests to forward proxy format
        3. Selects next authentication configuration using round-robin load balancing
        4. Generates JWT token for the selected configuration
        5. Modifies request headers to include authentication
        6. Redirects request to Flow LLM Proxy HTTPS endpoint

        Args:
            request: The incoming HTTP request from the client

        Returns:
            Modified request with authentication headers, or None if request should be rejected

        Note:
            Returning None prevents upstream connection establishment and rejects the request.
        """
        try:
            # First, handle reverse proxy mode by converting path-only requests to full URLs
            # This must be done BEFORE validation
            if (
                request.path
                and not request.path.startswith(b"http://")
                and not request.path.startswith(b"https://")
            ):
                # This is a reverse proxy request (path only, no full URL)
                # Convert it to forward proxy format by prepending the target base URL
                original_path = request.path.decode()
                target_url = f"{self.request_forwarder.target_base_url}{original_path}"
                request.set_url(target_url.encode())
                self.logger.debug(
                    f"Converted reverse proxy request: {original_path} -> {target_url}"
                )

            # Validate request before processing
            if not self.request_forwarder.validate_request(request):
                self.logger.error("Request validation failed - rejecting request")
                return None

            # Get next configuration using round-robin load balancing
            config = self.load_balancer.get_next_config()
            config_name = config.get("name", config.get("clientId", "unknown"))

            self.logger.debug(
                f"Processing request with method={request.method.decode() if request.method else 'UNKNOWN'}, "
                f"path={request.path.decode() if request.path else 'UNKNOWN'}"
            )

            # Generate JWT token for the selected configuration
            try:
                jwt_token = self.jwt_generator.generate_token(config)
            except ValueError as token_error:
                # Token generation failed for this config, mark it as failed
                self.logger.error(
                    f"Token generation failed for config '{config_name}': {str(token_error)}"
                )
                self.load_balancer.mark_config_failed(config)

                # Try to get next available config
                try:
                    config = self.load_balancer.get_next_config()
                    config_name = config.get("name", config.get("clientId", "unknown"))
                    jwt_token = self.jwt_generator.generate_token(config)
                    self.logger.info(
                        f"Failover successful - using config '{config_name}'"
                    )
                except (RuntimeError, ValueError) as failover_error:
                    self.logger.error(
                        f"Failover failed: {str(failover_error)} - rejecting request"
                    )
                    return None

            # Modify request headers to include authentication
            modified_request = self.request_forwarder.modify_request_headers(
                request, jwt_token, config_name
            )

            target_url = request.path.decode() if request.path else "unknown"
            self.logger.info(
                f"Request processed successfully with config: '{config_name}', "
                f"forwarding to: {target_url}"
            )
            return modified_request

        except RuntimeError as e:
            # No available configurations
            self.logger.error(
                f"No available authentication configurations: {str(e)} - rejecting request"
            )
            return None
        except ValueError as e:
            # Invalid request or processing failed
            self.logger.error(
                f"Request processing failed: {str(e)} - rejecting request"
            )
            return None
        except Exception as e:
            self.logger.error(
                f"Unexpected error processing request: {str(e)} - rejecting request",
                exc_info=True,
            )
            return None

    def handle_client_request(self, request: HttpParser) -> HttpParser | None:
        """Handle client request and add authentication information.

        This method is called for each client request and delegates to
        before_upstream_connection for processing.

        Args:
            request: The client HTTP request

        Returns:
            Processed request with authentication or None if request should be rejected
        """
        return self.before_upstream_connection(request)

    def handle_upstream_chunk(self, chunk: memoryview) -> memoryview | None:
        """Handle upstream response data with transparent pass-through.

        This method is called by proxy.py for each chunk of data received from
        the upstream server. It implements transparent response forwarding,
        supporting both regular and streaming responses.

        The method performs the following:
        1. Logs chunk reception for monitoring
        2. Passes chunk through transparently without modification
        3. Supports streaming responses by not buffering data

        Args:
            chunk: Response data chunk from upstream server

        Returns:
            Unmodified chunk for transparent pass-through, or None to drop the chunk

        Note:
            This implementation ensures response data is forwarded to the client
            exactly as received from Flow LLM Proxy, maintaining data integrity
            and supporting streaming responses.
        """
        try:
            if chunk:
                chunk_size = len(chunk)
                self.logger.debug(f"Received upstream chunk of {chunk_size} bytes")

                # Use request forwarder to handle response with transparent pass-through
                # This supports both regular and streaming responses
                return self.request_forwarder.handle_response_chunk(chunk)

            self.logger.debug("Received empty chunk from upstream")
            return chunk

        except Exception as e:
            self.logger.error(f"Error handling upstream chunk: {str(e)}", exc_info=True)
            # Return chunk anyway to maintain connection stability
            return chunk

    def on_upstream_connection_close(self) -> None:
        """Handle upstream connection closure.

        This method is called when the upstream connection is closed.
        It performs cleanup and logging.
        """
        self.logger.info("Upstream connection closed")

    def on_access_log(self, context: dict[str, Any]) -> dict[str, Any] | None:
        """Override access log to include plugin-specific information.

        Args:
            context: Access log context dictionary

        Returns:
            Modified context or None to prevent further plugin invocations
        """
        try:
            # Add plugin-specific context
            context["plugin"] = "FlowProxyPlugin"
            context["load_balancer_stats"] = {
                "available_configs": self.load_balancer.available_count,
                "failed_configs": self.load_balancer.failed_count,
                "total_requests": self.load_balancer.total_requests,
            }

            self.logger.debug(f"Access log context: {context}")
            return context

        except Exception as e:
            self.logger.error(f"Error in access log handler: {str(e)}", exc_info=True)
            return context
