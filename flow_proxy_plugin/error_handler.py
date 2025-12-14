"""Unified error handling system for FlowProxyPlugin.

This module provides standardized error handling, error response formatting,
and error code mappings for the Flow Proxy Plugin.
"""

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ErrorCode(Enum):
    """Standard error codes for the plugin."""

    # Configuration errors (1xxx)
    CONFIG_FILE_NOT_FOUND = "CONFIG_1001"
    CONFIG_INVALID_FORMAT = "CONFIG_1002"
    CONFIG_MISSING_FIELDS = "CONFIG_1003"
    CONFIG_EMPTY_ARRAY = "CONFIG_1004"
    CONFIG_VALIDATION_FAILED = "CONFIG_1005"

    # Authentication errors (2xxx)
    AUTH_TOKEN_GENERATION_FAILED = "AUTH_2001"
    AUTH_INVALID_CREDENTIALS = "AUTH_2002"
    AUTH_NO_AVAILABLE_CONFIGS = "AUTH_2003"
    AUTH_ALL_CONFIGS_FAILED = "AUTH_2004"

    # Request errors (3xxx)
    REQUEST_INVALID_FORMAT = "REQUEST_3001"
    REQUEST_MISSING_METHOD = "REQUEST_3002"
    REQUEST_MISSING_PATH = "REQUEST_3003"
    REQUEST_VALIDATION_FAILED = "REQUEST_3004"

    # Network errors (4xxx)
    NETWORK_CONNECTION_FAILED = "NETWORK_4001"
    NETWORK_TIMEOUT = "NETWORK_4002"
    NETWORK_UPSTREAM_UNAVAILABLE = "NETWORK_4003"
    NETWORK_DNS_RESOLUTION_FAILED = "NETWORK_4004"

    # Internal errors (5xxx)
    INTERNAL_UNEXPECTED_ERROR = "INTERNAL_5001"
    INTERNAL_PROCESSING_FAILED = "INTERNAL_5002"


class ErrorHandler:
    """Unified error handler for the plugin.

    Provides standardized error response formatting, error logging,
    and error code management.
    """

    # Error code to HTTP status code mapping
    ERROR_STATUS_MAP = {
        # Configuration errors -> 500 Internal Server Error
        ErrorCode.CONFIG_FILE_NOT_FOUND: 500,
        ErrorCode.CONFIG_INVALID_FORMAT: 500,
        ErrorCode.CONFIG_MISSING_FIELDS: 500,
        ErrorCode.CONFIG_EMPTY_ARRAY: 500,
        ErrorCode.CONFIG_VALIDATION_FAILED: 500,
        # Authentication errors -> 500 Internal Server Error (server-side auth issue)
        ErrorCode.AUTH_TOKEN_GENERATION_FAILED: 500,
        ErrorCode.AUTH_INVALID_CREDENTIALS: 500,
        ErrorCode.AUTH_NO_AVAILABLE_CONFIGS: 503,  # Service Unavailable
        ErrorCode.AUTH_ALL_CONFIGS_FAILED: 503,  # Service Unavailable
        # Request errors -> 400 Bad Request
        ErrorCode.REQUEST_INVALID_FORMAT: 400,
        ErrorCode.REQUEST_MISSING_METHOD: 400,
        ErrorCode.REQUEST_MISSING_PATH: 400,
        ErrorCode.REQUEST_VALIDATION_FAILED: 400,
        # Network errors -> 502 Bad Gateway
        ErrorCode.NETWORK_CONNECTION_FAILED: 502,
        ErrorCode.NETWORK_TIMEOUT: 504,  # Gateway Timeout
        ErrorCode.NETWORK_UPSTREAM_UNAVAILABLE: 502,
        ErrorCode.NETWORK_DNS_RESOLUTION_FAILED: 502,
        # Internal errors -> 500 Internal Server Error
        ErrorCode.INTERNAL_UNEXPECTED_ERROR: 500,
        ErrorCode.INTERNAL_PROCESSING_FAILED: 500,
    }

    # Error code to user-friendly message mapping
    ERROR_MESSAGE_MAP = {
        # Configuration errors
        ErrorCode.CONFIG_FILE_NOT_FOUND: "Configuration file not found",
        ErrorCode.CONFIG_INVALID_FORMAT: "Invalid configuration file format",
        ErrorCode.CONFIG_MISSING_FIELDS: "Configuration missing required fields",
        ErrorCode.CONFIG_EMPTY_ARRAY: "Configuration array is empty",
        ErrorCode.CONFIG_VALIDATION_FAILED: "Configuration validation failed",
        # Authentication errors
        ErrorCode.AUTH_TOKEN_GENERATION_FAILED: "Failed to generate authentication token",
        ErrorCode.AUTH_INVALID_CREDENTIALS: "Invalid authentication credentials",
        ErrorCode.AUTH_NO_AVAILABLE_CONFIGS: "No authentication configurations available",
        ErrorCode.AUTH_ALL_CONFIGS_FAILED: "All authentication configurations have failed",
        # Request errors
        ErrorCode.REQUEST_INVALID_FORMAT: "Invalid request format",
        ErrorCode.REQUEST_MISSING_METHOD: "Request missing HTTP method",
        ErrorCode.REQUEST_MISSING_PATH: "Request missing path",
        ErrorCode.REQUEST_VALIDATION_FAILED: "Request validation failed",
        # Network errors
        ErrorCode.NETWORK_CONNECTION_FAILED: "Failed to connect to upstream server",
        ErrorCode.NETWORK_TIMEOUT: "Request to upstream server timed out",
        ErrorCode.NETWORK_UPSTREAM_UNAVAILABLE: "Upstream server is unavailable",
        ErrorCode.NETWORK_DNS_RESOLUTION_FAILED: "Failed to resolve upstream server address",
        # Internal errors
        ErrorCode.INTERNAL_UNEXPECTED_ERROR: "An unexpected error occurred",
        ErrorCode.INTERNAL_PROCESSING_FAILED: "Request processing failed",
    }

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Initialize error handler.

        Args:
            logger: Optional logger instance (creates new one if not provided)
        """
        self.logger = logger or logging.getLogger(__name__)

    def create_error_response(
        self,
        error_code: ErrorCode,
        details: str | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create standardized error response.

        Args:
            error_code: The error code enum
            details: Optional detailed error description
            additional_context: Optional additional context information

        Returns:
            Standardized error response dictionary
        """
        response: dict[str, Any] = {
            "error": error_code.value,
            "message": self.ERROR_MESSAGE_MAP.get(error_code, "An error occurred"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if details:
            response["details"] = details

        if additional_context:
            response["context"] = additional_context

        return response

    def format_error_response_http(
        self,
        error_code: ErrorCode,
        details: str | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> bytes:
        """Format error response as HTTP response bytes.

        Args:
            error_code: The error code enum
            details: Optional detailed error description
            additional_context: Optional additional context information

        Returns:
            Complete HTTP response as bytes
        """
        status_code = self.ERROR_STATUS_MAP.get(error_code, 500)
        status_text = self._get_status_text(status_code)

        error_body = self.create_error_response(error_code, details, additional_context)
        body_json = json.dumps(error_body, indent=2)

        response = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_json)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body_json}"
        )

        return response.encode("utf-8")

    def log_error(
        self,
        error_code: ErrorCode,
        details: str | None = None,
        exception: Exception | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> None:
        """Log error with standardized format.

        Args:
            error_code: The error code enum
            details: Optional detailed error description
            exception: Optional exception object
            additional_context: Optional additional context information
        """
        message = self.ERROR_MESSAGE_MAP.get(error_code, "Unknown error")
        log_message = f"[{error_code.value}] {message}"

        if details:
            log_message += f" - {details}"

        if additional_context:
            context_str = ", ".join(f"{k}={v}" for k, v in additional_context.items())
            log_message += f" | Context: {context_str}"

        # Determine log level based on error type
        if error_code.value.startswith("CONFIG") or error_code.value.startswith(
            "INTERNAL"
        ):
            self.logger.error(log_message, exc_info=exception)
        elif error_code.value.startswith("AUTH"):
            self.logger.error(log_message, exc_info=exception)
        elif error_code.value.startswith("NETWORK"):
            self.logger.warning(log_message, exc_info=exception)
        elif error_code.value.startswith("REQUEST"):
            self.logger.info(log_message)
        else:
            self.logger.error(log_message, exc_info=exception)

    def handle_exception(
        self,
        exception: Exception,
        context: str | None = None,
        additional_context: dict[str, Any] | None = None,
    ) -> tuple[ErrorCode, str]:
        """Handle exception and map to appropriate error code.

        Args:
            exception: The exception to handle
            context: Optional context description
            additional_context: Optional additional context information

        Returns:
            Tuple of (error_code, details_message)
        """
        details = str(exception)
        if context:
            details = f"{context}: {details}"

        # Map exception types to error codes
        error_code = self._map_exception_to_error_code(exception)

        # Log the error
        self.log_error(error_code, details, exception, additional_context)

        return error_code, details

    def _map_exception_to_error_code(self, exception: Exception) -> ErrorCode:
        """Map exception type to error code.

        Args:
            exception: The exception to map

        Returns:
            Appropriate error code
        """
        # Simple type mappings
        type_mappings = {
            FileNotFoundError: ErrorCode.CONFIG_FILE_NOT_FOUND,
            json.JSONDecodeError: ErrorCode.CONFIG_INVALID_FORMAT,
            ConnectionError: ErrorCode.NETWORK_CONNECTION_FAILED,
            TimeoutError: ErrorCode.NETWORK_TIMEOUT,
        }

        # Check simple type mappings first
        for exc_type, error_code in type_mappings.items():
            if isinstance(exception, exc_type):
                return error_code

        # Handle complex mappings that require message inspection
        if isinstance(exception, ValueError):
            return self._map_value_error(exception)
        if isinstance(exception, RuntimeError):
            return self._map_runtime_error(exception)

        return ErrorCode.INTERNAL_UNEXPECTED_ERROR

    def _map_value_error(self, exception: ValueError) -> ErrorCode:
        """Map ValueError to specific error code based on message.

        Args:
            exception: The ValueError to map

        Returns:
            Appropriate error code
        """
        error_msg = str(exception).lower()
        if "config" in error_msg or "secret" in error_msg:
            return ErrorCode.CONFIG_VALIDATION_FAILED
        if "token" in error_msg or "auth" in error_msg:
            return ErrorCode.AUTH_TOKEN_GENERATION_FAILED
        if "request" in error_msg:
            return ErrorCode.REQUEST_VALIDATION_FAILED
        return ErrorCode.INTERNAL_PROCESSING_FAILED

    def _map_runtime_error(self, exception: RuntimeError) -> ErrorCode:
        """Map RuntimeError to specific error code based on message.

        Args:
            exception: The RuntimeError to map

        Returns:
            Appropriate error code
        """
        error_msg = str(exception).lower()
        if "config" in error_msg or "authentication" in error_msg:
            return ErrorCode.AUTH_NO_AVAILABLE_CONFIGS
        return ErrorCode.INTERNAL_UNEXPECTED_ERROR

    def _get_status_text(self, status_code: int) -> str:
        """Get HTTP status text for status code.

        Args:
            status_code: HTTP status code

        Returns:
            Status text string
        """
        status_texts = {
            400: "Bad Request",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }
        return status_texts.get(status_code, "Error")
