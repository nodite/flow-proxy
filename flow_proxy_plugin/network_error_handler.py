"""Network error handling with timeout and retry support.

This module provides specialized network error handling including
connection timeouts, retry mechanisms, and upstream availability checks.
"""

import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from .error_handler import ErrorCode, ErrorHandler

T = TypeVar("T")


class NetworkErrorHandler:
    """Handles network-related errors with timeout and retry support."""

    def __init__(
        self,
        logger: logging.Logger | None = None,
        default_timeout: float = 30.0,
        max_retries: int = 0,
        retry_delay: float = 1.0,
    ) -> None:
        """Initialize network error handler.

        Args:
            logger: Optional logger instance
            default_timeout: Default timeout in seconds for network operations
            max_retries: Maximum number of retry attempts (0 = no retries)
            retry_delay: Delay in seconds between retry attempts
        """
        self.logger = logger or logging.getLogger(__name__)
        self.error_handler = ErrorHandler(self.logger)
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self.logger.info(
            f"NetworkErrorHandler initialized with timeout={default_timeout}s, "
            f"max_retries={max_retries}, retry_delay={retry_delay}s"
        )

    def handle_connection_error(
        self,
        error: Exception,
        target_url: str,
        context: dict[str, Any] | None = None,
    ) -> bytes:
        """Handle connection errors to upstream server.

        Args:
            error: The connection error that occurred
            target_url: The target URL that failed
            context: Optional additional context

        Returns:
            HTTP error response as bytes
        """
        additional_context = {"target_url": target_url}
        if context:
            additional_context.update(context)

        error_code = ErrorCode.NETWORK_CONNECTION_FAILED
        details = f"Failed to connect to {target_url}: {str(error)}"

        self.error_handler.log_error(error_code, details, error, additional_context)

        return self.error_handler.format_error_response_http(
            error_code, details, additional_context
        )

    def handle_timeout_error(
        self,
        error: Exception,
        target_url: str,
        timeout: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> bytes:
        """Handle timeout errors for upstream requests.

        Args:
            error: The timeout error that occurred
            target_url: The target URL that timed out
            timeout: The timeout value that was exceeded
            context: Optional additional context

        Returns:
            HTTP error response as bytes
        """
        additional_context = {
            "target_url": target_url,
            "timeout": timeout or self.default_timeout,
        }
        if context:
            additional_context.update(context)

        error_code = ErrorCode.NETWORK_TIMEOUT
        details = (
            f"Request to {target_url} timed out after "
            f"{timeout or self.default_timeout} seconds: {str(error)}"
        )

        self.error_handler.log_error(error_code, details, error, additional_context)

        return self.error_handler.format_error_response_http(
            error_code, details, additional_context
        )

    def handle_upstream_unavailable(
        self,
        target_url: str,
        reason: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> bytes:
        """Handle upstream server unavailable errors.

        Args:
            target_url: The target URL that is unavailable
            reason: Optional reason for unavailability
            context: Optional additional context

        Returns:
            HTTP error response as bytes
        """
        additional_context = {"target_url": target_url}
        if context:
            additional_context.update(context)

        error_code = ErrorCode.NETWORK_UPSTREAM_UNAVAILABLE
        details = f"Upstream server {target_url} is unavailable"
        if reason:
            details += f": {reason}"

        self.error_handler.log_error(error_code, details, None, additional_context)

        return self.error_handler.format_error_response_http(
            error_code, details, additional_context
        )

    def execute_with_retry(
        self,
        operation: Callable[[], T],
        operation_name: str,
        max_retries: int | None = None,
        retry_delay: float | None = None,
    ) -> T:
        """Execute an operation with retry logic.

        Args:
            operation: The operation to execute
            operation_name: Name of the operation for logging
            max_retries: Maximum retry attempts (uses default if None)
            retry_delay: Delay between retries (uses default if None)

        Returns:
            Result of the operation

        Raises:
            Exception: The last exception if all retries fail
        """
        max_attempts = (
            max_retries if max_retries is not None else self.max_retries
        ) + 1
        delay = retry_delay if retry_delay is not None else self.retry_delay

        last_exception = None

        for attempt in range(1, max_attempts + 1):
            try:
                self.logger.debug(
                    f"Executing {operation_name} (attempt {attempt}/{max_attempts})"
                )
                result = operation()
                if attempt > 1:
                    self.logger.info(f"{operation_name} succeeded on attempt {attempt}")
                return result

            except Exception as e:
                last_exception = e
                self.logger.warning(
                    f"{operation_name} failed on attempt {attempt}/{max_attempts}: {str(e)}"
                )

                # Don't sleep after the last attempt
                if attempt < max_attempts:
                    self.logger.debug(f"Retrying after {delay} seconds...")
                    time.sleep(delay)

        # All retries exhausted
        self.logger.error(f"{operation_name} failed after {max_attempts} attempts")
        if last_exception:
            raise last_exception
        raise RuntimeError(f"{operation_name} failed after all retry attempts")

    def execute_with_timeout(
        self,
        operation: Callable[[], T],
        operation_name: str,
        timeout: float | None = None,
    ) -> T:
        """Execute an operation with timeout.

        Note: This is a basic implementation. For true timeout support,
        consider using threading or asyncio with timeout mechanisms.

        Args:
            operation: The operation to execute
            operation_name: Name of the operation for logging
            timeout: Timeout in seconds (uses default if None)

        Returns:
            Result of the operation

        Raises:
            TimeoutError: If operation exceeds timeout
            Exception: Any exception raised by the operation
        """
        timeout_value = timeout if timeout is not None else self.default_timeout

        self.logger.debug(f"Executing {operation_name} with timeout={timeout_value}s")

        # Note: This is a simplified implementation
        # For production use, consider using threading.Timer or asyncio.wait_for
        try:
            result = operation()
            return result
        except TimeoutError:
            self.logger.error(
                f"{operation_name} timed out after {timeout_value} seconds"
            )
            raise
        except Exception as e:
            self.logger.error(f"{operation_name} failed: {str(e)}")
            raise

    def check_upstream_availability(
        self, target_url: str, timeout: float | None = None
    ) -> bool:
        """Check if upstream server is available.

        This is a basic availability check. For production use,
        consider implementing health check endpoints.

        Args:
            target_url: The target URL to check
            timeout: Timeout for the check (uses default if None)

        Returns:
            True if upstream appears available, False otherwise
        """
        timeout_value = timeout if timeout is not None else self.default_timeout

        self.logger.debug(
            f"Checking upstream availability: {target_url} (timeout={timeout_value}s)"
        )

        # This is a placeholder for actual availability checking
        # In a real implementation, you might:
        # 1. Send a HEAD request to a health check endpoint
        # 2. Check DNS resolution
        # 3. Attempt a TCP connection
        # 4. Check response time

        # For now, we'll just log and return True
        # The actual connection attempt will reveal availability
        self.logger.debug(f"Upstream {target_url} assumed available")
        return True

    def get_retry_config(self) -> dict[str, Any]:
        """Get current retry configuration.

        Returns:
            Dictionary with retry configuration
        """
        return {
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
            "default_timeout": self.default_timeout,
        }

    def update_retry_config(
        self,
        max_retries: int | None = None,
        retry_delay: float | None = None,
        default_timeout: float | None = None,
    ) -> None:
        """Update retry configuration.

        Args:
            max_retries: New max retries value
            retry_delay: New retry delay value
            default_timeout: New default timeout value
        """
        if max_retries is not None:
            self.max_retries = max_retries
            self.logger.info(f"Updated max_retries to {max_retries}")

        if retry_delay is not None:
            self.retry_delay = retry_delay
            self.logger.info(f"Updated retry_delay to {retry_delay}s")

        if default_timeout is not None:
            self.default_timeout = default_timeout
            self.logger.info(f"Updated default_timeout to {default_timeout}s")
