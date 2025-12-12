"""Request forwarder for handling HTTP requests to Flow LLM Proxy."""

import logging

from proxy.http.parser import HttpParser


class RequestForwarder:
    """Handles request forwarding to Flow LLM Proxy."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Initialize request forwarder.

        Args:
            logger: Optional logger instance (creates new one if not provided)
        """
        self.logger = logger or logging.getLogger(__name__)
        self.target_base_url = "https://flow.ciandt.com/flow-llm-proxy"
        self.target_host = "flow.ciandt.com"

        self.logger.info(
            f"RequestForwarder initialized with target: {self.target_base_url}"
        )

    def modify_request_headers(
        self,
        request: HttpParser,
        jwt_token: str,
        config_name: str | None = None,
    ) -> HttpParser:
        """Modify request headers to add authentication information.

        Args:
            request: The HTTP request to modify
            jwt_token: The JWT token to add to Authorization header
            config_name: Optional configuration name for logging

        Returns:
            Modified request with updated headers

        Raises:
            ValueError: If request or jwt_token is invalid
        """
        if request is None:
            error_msg = "request is None"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        if not jwt_token or not jwt_token.strip():
            error_msg = "jwt_token is empty"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Ensure headers exist
        if request.headers is None:
            error_msg = "request.headers is None"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Remove any existing Authorization headers (case-insensitive)
        headers_to_remove = []
        for header_name in request.headers:
            if header_name.lower() == b"authorization":
                headers_to_remove.append(header_name)

        for header_name in headers_to_remove:
            del request.headers[header_name]

        # Add new Authorization header with Bearer token
        request.headers[b"Authorization"] = (f"Bearer {jwt_token}".encode(), b"")

        # Update Host header to target host
        request.headers[b"Host"] = (self.target_host.encode(), b"")

        config_info = f" (config: {config_name})" if config_name else ""
        self.logger.info(
            f"Modified request headers with JWT token{config_info}, "
            f"target host: {self.target_host}"
        )

        return request

    def get_target_url(self, original_path: str) -> str:
        """Get target Flow LLM Proxy URL.

        Args:
            original_path: Original request path

        Returns:
            Complete target URL

        Raises:
            ValueError: If original_path is empty
        """
        if not original_path or not original_path.strip():
            error_msg = "original_path is empty"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Ensure path starts with /
        if not original_path.startswith("/"):
            original_path = f"/{original_path}"

        target_url = f"{self.target_base_url}{original_path}"
        self.logger.debug(f"Target URL: {target_url}")

        return target_url

    def handle_response_chunk(self, chunk: memoryview) -> memoryview:
        """Handle upstream response data with transparent pass-through.

        This method implements transparent response forwarding, passing
        response data unchanged to the client.

        Args:
            chunk: Response data chunk from upstream

        Returns:
            Unmodified chunk (transparent pass-through)
        """
        # Transparent pass-through - return chunk unchanged
        return chunk

    def handle_streaming_response(self, chunk: memoryview) -> memoryview:
        """Handle streaming response data.

        This method supports streaming responses by passing chunks
        through transparently without buffering.

        Args:
            chunk: Streaming response data chunk

        Returns:
            Unmodified chunk for streaming
        """
        # Streaming support - pass through without buffering
        return chunk

    def handle_forwarding_error(
        self, error: Exception, error_type: str = "general"
    ) -> memoryview:
        """Handle forwarding errors and generate appropriate error responses.

        Args:
            error: The exception that occurred
            error_type: Type of error (network, timeout, invalid_request, general)

        Returns:
            Error response as memoryview
        """
        error_msg = str(error)
        self.logger.error(f"Forwarding error ({error_type}): {error_msg}")

        # Determine appropriate status code and message
        if error_type == "network" or isinstance(error, ConnectionError):
            status_code = 502
            status_text = "Bad Gateway"
            message = "Unable to connect to Flow LLM Proxy"
        elif error_type == "timeout" or isinstance(error, TimeoutError):
            status_code = 502
            status_text = "Bad Gateway"
            message = "Request to Flow LLM Proxy timed out"
        elif error_type == "invalid_request" or isinstance(error, ValueError):
            status_code = 400
            status_text = "Bad Request"
            message = "Invalid request format"
        else:
            status_code = 500
            status_text = "Internal Server Error"
            message = "An unexpected error occurred"

        # Create error response
        error_response = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f'{{"error": "{message}", "details": "{error_msg}"}}'
        )

        return memoryview(error_response.encode())

    def validate_request(self, request: HttpParser | None) -> bool:
        """Validate HTTP request before processing.

        Args:
            request: The HTTP request to validate

        Returns:
            True if request is valid, False otherwise
        """
        if request is None:
            self.logger.error("Request validation failed: request is None")
            return False

        if not hasattr(request, "method") or request.method is None:
            self.logger.error("Request validation failed: missing method")
            return False

        if not hasattr(request, "path") or request.path is None:
            self.logger.error("Request validation failed: missing path")
            return False

        return True
