"""Main FlowProxyPlugin class implementation."""

import logging
from typing import Any

from proxy.http.parser import HttpParser
from proxy.http.plugin import HttpProtocolHandlerPlugin

from .config import SecretsManager
from .jwt_generator import JWTGenerator
from .load_balancer import LoadBalancer
from .request_forwarder import RequestForwarder


class FlowProxyPlugin(HttpProtocolHandlerPlugin):
    """Flow LLM Proxy authentication plugin main class.

    This plugin handles authentication token generation and request forwarding
    to Flow LLM Proxy service with round-robin load balancing.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize plugin, load authentication configurations."""
        super().__init__(*args, **kwargs)

        self.logger = logging.getLogger(__name__)

        # Initialize components
        self.secrets_manager = SecretsManager()
        self.configs = self.secrets_manager.load_secrets("secrets.json")

        self.load_balancer = LoadBalancer(self.configs, self.logger)
        self.jwt_generator = JWTGenerator(self.logger)
        self.request_forwarder = RequestForwarder(self.logger)

        self.logger.info(
            f"FlowProxyPlugin initialized with {len(self.configs)} configurations"
        )

    def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
        """Process request before establishing upstream connection.

        Args:
            request: The incoming HTTP request

        Returns:
            Modified request with authentication or None if request should be rejected
        """
        try:
            # Validate request before processing
            if not self.request_forwarder.validate_request(request):
                self.logger.error("Request validation failed")
                return None

            # Get next configuration using round-robin
            config = self.load_balancer.get_next_config()
            config_name = config.get("name", config.get("clientId", "unknown"))

            # Generate JWT token
            jwt_token = self.jwt_generator.generate_token(config)

            # Modify request headers and target
            modified_request = self.request_forwarder.modify_request_headers(
                request, jwt_token, config_name
            )

            self.logger.info(
                f"Request processed successfully with config: {config_name}"
            )
            return modified_request

        except RuntimeError as e:
            # No available configurations
            self.logger.error(f"No available configurations: {str(e)}")
            return None
        except ValueError as e:
            # Invalid request or token generation failed
            self.logger.error(f"Request processing failed: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error processing request: {str(e)}")
            return None

    def handle_client_request(self, request: HttpParser) -> HttpParser | None:
        """Handle client request, add authentication information.

        Args:
            request: The client HTTP request

        Returns:
            Processed request or None if request should be rejected
        """
        return self.before_upstream_connection(request)

    def handle_upstream_chunk(self, chunk: memoryview) -> memoryview:
        """Handle upstream response data.

        This method supports transparent response forwarding and streaming responses.

        Args:
            chunk: Response data chunk from upstream

        Returns:
            Unmodified chunk (transparent pass-through)
        """
        # Use request forwarder to handle response with transparent pass-through
        return self.request_forwarder.handle_response_chunk(chunk)
