"""Request forwarding component for Flow LLM Proxy."""

import logging
from urllib.parse import urljoin

from proxy.http.parser import HttpParser


class RequestForwarder:
    """Handles request forwarding to Flow LLM Proxy."""

    def __init__(self) -> None:
        """Initialize request forwarder."""
        self.logger = logging.getLogger(__name__)
        self.target_base_url = "https://flow.ciandt.com/flow-llm-proxy"

    def modify_request_headers(self, request: HttpParser, jwt_token: str) -> HttpParser:
        """Modify request headers, add authentication information.

        Args:
            request: Original HTTP request
            jwt_token: Generated JWT token

        Returns:
            Modified HTTP request with authentication
        """
        # Remove any existing authorization headers
        if b"authorization" in request.headers:
            del request.headers[b"authorization"]
        if b"Authorization" in request.headers:
            del request.headers[b"Authorization"]

        # Add new authorization header with JWT token
        auth_header = f"Bearer {jwt_token}"
        request.headers[b"Authorization"] = auth_header.encode("utf-8")

        # Update host header to target
        request.headers[b"Host"] = b"flow.ciandt.com"

        self.logger.debug("Request headers modified with JWT authentication")

        return request

    def get_target_url(self, original_path: str) -> str:
        """Get target Flow LLM Proxy URL.

        Args:
            original_path: Original request path

        Returns:
            Complete target URL
        """
        # Ensure path starts with /
        if not original_path.startswith("/"):
            original_path = "/" + original_path

        target_url = urljoin(self.target_base_url, original_path)

        self.logger.debug(f"Target URL: {target_url}")

        return target_url

    def handle_forwarding_error(self, error: Exception) -> None:
        """Handle errors during request forwarding process.

        Args:
            error: Exception that occurred during forwarding
        """
        self.logger.error(f"Request forwarding error: {str(error)}")
        # Error handling will be implemented in the main plugin class
