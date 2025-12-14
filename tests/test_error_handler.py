"""Tests for error handler module."""

import json
import logging
from datetime import datetime

import pytest

from flow_proxy_plugin.error_handler import ErrorCode, ErrorHandler


@pytest.fixture
def error_handler() -> ErrorHandler:
    """Create error handler instance for testing."""
    logger = logging.getLogger("test_error_handler")
    return ErrorHandler(logger)


def test_error_handler_initialization(error_handler: ErrorHandler) -> None:
    """Test error handler initialization."""
    assert error_handler is not None
    assert error_handler.logger is not None


def test_create_error_response_basic(error_handler: ErrorHandler) -> None:
    """Test creating basic error response."""
    response = error_handler.create_error_response(ErrorCode.CONFIG_FILE_NOT_FOUND)

    assert "error" in response
    assert response["error"] == "CONFIG_1001"
    assert "message" in response
    assert response["message"] == "Configuration file not found"
    assert "timestamp" in response


def test_create_error_response_with_details(error_handler: ErrorHandler) -> None:
    """Test creating error response with details."""
    response = error_handler.create_error_response(
        ErrorCode.AUTH_TOKEN_GENERATION_FAILED,
        details="Invalid client secret",
    )

    assert response["error"] == "AUTH_2001"
    assert "details" in response
    assert response["details"] == "Invalid client secret"


def test_create_error_response_with_context(error_handler: ErrorHandler) -> None:
    """Test creating error response with additional context."""
    context = {"config_name": "test_config", "attempt": 1}
    response = error_handler.create_error_response(
        ErrorCode.NETWORK_TIMEOUT, additional_context=context
    )

    assert "context" in response
    assert response["context"]["config_name"] == "test_config"
    assert response["context"]["attempt"] == 1


def test_format_error_response_http(error_handler: ErrorHandler) -> None:
    """Test formatting error response as HTTP."""
    http_response = error_handler.format_error_response_http(
        ErrorCode.REQUEST_INVALID_FORMAT
    )

    assert isinstance(http_response, bytes)
    response_str = http_response.decode("utf-8")

    # Check HTTP status line
    assert "HTTP/1.1 400 Bad Request" in response_str

    # Check headers
    assert "Content-Type: application/json" in response_str
    assert "Content-Length:" in response_str
    assert "Connection: close" in response_str

    # Check body contains error info
    assert "REQUEST_3001" in response_str
    assert "Invalid request format" in response_str


def test_error_status_mapping(error_handler: ErrorHandler) -> None:
    """Test error code to HTTP status mapping."""
    # Configuration errors -> 500
    assert ErrorHandler.ERROR_STATUS_MAP[ErrorCode.CONFIG_FILE_NOT_FOUND] == 500

    # Request errors -> 400
    assert ErrorHandler.ERROR_STATUS_MAP[ErrorCode.REQUEST_INVALID_FORMAT] == 400

    # Network errors -> 502/504
    assert ErrorHandler.ERROR_STATUS_MAP[ErrorCode.NETWORK_CONNECTION_FAILED] == 502
    assert ErrorHandler.ERROR_STATUS_MAP[ErrorCode.NETWORK_TIMEOUT] == 504

    # Auth unavailable -> 503
    assert ErrorHandler.ERROR_STATUS_MAP[ErrorCode.AUTH_NO_AVAILABLE_CONFIGS] == 503


def test_handle_exception_file_not_found(error_handler: ErrorHandler) -> None:
    """Test handling FileNotFoundError exception."""
    exception = FileNotFoundError("secrets.json not found")
    error_code, details = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.CONFIG_FILE_NOT_FOUND
    assert "secrets.json not found" in details


def test_handle_exception_json_decode(error_handler: ErrorHandler) -> None:
    """Test handling JSONDecodeError exception."""
    exception = json.JSONDecodeError("Invalid JSON", "", 0)
    error_code, _ = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.CONFIG_INVALID_FORMAT


def test_handle_exception_value_error_config(error_handler: ErrorHandler) -> None:
    """Test handling ValueError for config errors."""
    exception = ValueError("Invalid config format")
    error_code, details = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.CONFIG_VALIDATION_FAILED
    assert "Invalid config format" in details


def test_handle_exception_value_error_token(error_handler: ErrorHandler) -> None:
    """Test handling ValueError for token errors."""
    exception = ValueError("Token generation failed")
    error_code, _ = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.AUTH_TOKEN_GENERATION_FAILED


def test_handle_exception_connection_error(error_handler: ErrorHandler) -> None:
    """Test handling ConnectionError exception."""
    exception = ConnectionError("Connection refused")
    error_code, details = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.NETWORK_CONNECTION_FAILED
    assert "Connection refused" in details


def test_handle_exception_timeout_error(error_handler: ErrorHandler) -> None:
    """Test handling TimeoutError exception."""
    exception = TimeoutError("Request timed out")
    error_code, details = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.NETWORK_TIMEOUT
    assert "Request timed out" in details


def test_handle_exception_runtime_error_config(error_handler: ErrorHandler) -> None:
    """Test handling RuntimeError for config unavailability."""
    exception = RuntimeError("No available authentication configurations")
    error_code, _ = error_handler.handle_exception(exception)

    assert error_code == ErrorCode.AUTH_NO_AVAILABLE_CONFIGS


def test_handle_exception_with_context(error_handler: ErrorHandler) -> None:
    """Test handling exception with context."""
    exception = ValueError("Invalid request")
    context = {"request_id": "12345"}
    error_code, details = error_handler.handle_exception(
        exception, context="Processing request", additional_context=context
    )

    assert error_code == ErrorCode.REQUEST_VALIDATION_FAILED
    assert "Processing request" in details
    assert "Invalid request" in details


def test_log_error(
    error_handler: ErrorHandler, caplog: pytest.LogCaptureFixture
) -> None:
    """Test error logging."""
    with caplog.at_level(logging.ERROR):
        error_handler.log_error(
            ErrorCode.CONFIG_FILE_NOT_FOUND,
            details="File not found at path",
        )

    assert "CONFIG_1001" in caplog.text
    assert "Configuration file not found" in caplog.text
    assert "File not found at path" in caplog.text


def test_log_error_with_context(
    error_handler: ErrorHandler, caplog: pytest.LogCaptureFixture
) -> None:
    """Test error logging with context."""
    context = {"file_path": "/path/to/secrets.json"}

    with caplog.at_level(logging.ERROR):
        error_handler.log_error(
            ErrorCode.CONFIG_FILE_NOT_FOUND,
            details="File not found",
            additional_context=context,
        )

    assert "file_path=/path/to/secrets.json" in caplog.text


def test_all_error_codes_have_status_mapping(error_handler: ErrorHandler) -> None:
    """Test that all error codes have HTTP status mapping."""
    for error_code in ErrorCode:
        assert error_code in ErrorHandler.ERROR_STATUS_MAP


def test_all_error_codes_have_message_mapping(error_handler: ErrorHandler) -> None:
    """Test that all error codes have message mapping."""
    for error_code in ErrorCode:
        assert error_code in ErrorHandler.ERROR_MESSAGE_MAP


def test_error_response_timestamp_format(error_handler: ErrorHandler) -> None:
    """Test that error response timestamp is in ISO format."""
    response = error_handler.create_error_response(ErrorCode.INTERNAL_UNEXPECTED_ERROR)

    timestamp = response["timestamp"]
    # Should be able to parse as ISO format
    parsed = datetime.fromisoformat(timestamp)
    assert parsed is not None


def test_http_response_content_length_accurate(error_handler: ErrorHandler) -> None:
    """Test that HTTP response Content-Length header is accurate."""
    http_response = error_handler.format_error_response_http(
        ErrorCode.REQUEST_INVALID_FORMAT
    )

    response_str = http_response.decode("utf-8")
    headers, body = response_str.split("\r\n\r\n", 1)

    # Extract Content-Length from headers
    for line in headers.split("\r\n"):
        if line.startswith("Content-Length:"):
            content_length = int(line.split(":")[1].strip())
            assert content_length == len(body)
            break
    else:
        pytest.fail("Content-Length header not found")
