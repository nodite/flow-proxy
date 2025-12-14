"""Tests for network error handler module."""

import logging
import time

import pytest

from flow_proxy_plugin.network_error_handler import NetworkErrorHandler


@pytest.fixture
def network_handler() -> NetworkErrorHandler:
    """Create network error handler instance for testing."""
    logger = logging.getLogger("test_network_error_handler")
    return NetworkErrorHandler(
        logger, default_timeout=5.0, max_retries=2, retry_delay=0.1
    )


def test_network_handler_initialization(network_handler: NetworkErrorHandler) -> None:
    """Test network error handler initialization."""
    assert network_handler is not None
    assert network_handler.default_timeout == 5.0
    assert network_handler.max_retries == 2
    assert network_handler.retry_delay == 0.1


def test_handle_connection_error(network_handler: NetworkErrorHandler) -> None:
    """Test handling connection errors."""
    error = ConnectionError("Connection refused")
    target_url = "https://flow.ciandt.com/flow-llm-proxy"

    response = network_handler.handle_connection_error(error, target_url)

    assert isinstance(response, bytes)
    response_str = response.decode("utf-8")

    assert "502 Bad Gateway" in response_str
    assert "NETWORK_4001" in response_str
    assert target_url in response_str


def test_handle_connection_error_with_context(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test handling connection errors with context."""
    error = ConnectionError("Connection refused")
    target_url = "https://flow.ciandt.com/flow-llm-proxy"
    context = {"config_name": "test_config"}

    response = network_handler.handle_connection_error(error, target_url, context)

    response_str = response.decode("utf-8")
    assert "test_config" in response_str


def test_handle_timeout_error(network_handler: NetworkErrorHandler) -> None:
    """Test handling timeout errors."""
    error = TimeoutError("Request timed out")
    target_url = "https://flow.ciandt.com/flow-llm-proxy"

    response = network_handler.handle_timeout_error(error, target_url, timeout=10.0)

    assert isinstance(response, bytes)
    response_str = response.decode("utf-8")

    assert "504 Gateway Timeout" in response_str
    assert "NETWORK_4002" in response_str
    assert target_url in response_str
    assert "10" in response_str  # timeout value


def test_handle_timeout_error_default_timeout(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test handling timeout errors with default timeout."""
    error = TimeoutError("Request timed out")
    target_url = "https://flow.ciandt.com/flow-llm-proxy"

    response = network_handler.handle_timeout_error(error, target_url)

    response_str = response.decode("utf-8")
    assert "5" in response_str  # default timeout value


def test_handle_upstream_unavailable(network_handler: NetworkErrorHandler) -> None:
    """Test handling upstream unavailable errors."""
    target_url = "https://flow.ciandt.com/flow-llm-proxy"

    response = network_handler.handle_upstream_unavailable(target_url)

    assert isinstance(response, bytes)
    response_str = response.decode("utf-8")

    assert "502 Bad Gateway" in response_str
    assert "NETWORK_4003" in response_str
    assert target_url in response_str


def test_handle_upstream_unavailable_with_reason(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test handling upstream unavailable with reason."""
    target_url = "https://flow.ciandt.com/flow-llm-proxy"
    reason = "Service maintenance"

    response = network_handler.handle_upstream_unavailable(target_url, reason=reason)

    response_str = response.decode("utf-8")
    assert reason in response_str


def test_execute_with_retry_success_first_attempt(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test retry logic with success on first attempt."""
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        return "success"

    result = network_handler.execute_with_retry(operation, "test_operation")

    assert result == "success"
    assert call_count == 1


def test_execute_with_retry_success_after_failures(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test retry logic with success after initial failures."""
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ConnectionError("Temporary failure")
        return "success"

    result = network_handler.execute_with_retry(operation, "test_operation")

    assert result == "success"
    assert call_count == 2


def test_execute_with_retry_all_attempts_fail(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test retry logic when all attempts fail."""
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        raise ConnectionError("Persistent failure")

    with pytest.raises(ConnectionError, match="Persistent failure"):
        network_handler.execute_with_retry(operation, "test_operation")

    # Should try: initial + 2 retries = 3 attempts
    assert call_count == 3


def test_execute_with_retry_custom_max_retries(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test retry logic with custom max retries."""
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        raise ConnectionError("Failure")

    with pytest.raises(ConnectionError):
        network_handler.execute_with_retry(operation, "test_operation", max_retries=1)

    # Should try: initial + 1 retry = 2 attempts
    assert call_count == 2


def test_execute_with_retry_respects_delay(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test that retry logic respects delay between attempts."""
    call_times = []

    def operation() -> str:
        call_times.append(time.time())
        if len(call_times) < 2:
            raise ConnectionError("Temporary failure")
        return "success"

    network_handler.execute_with_retry(operation, "test_operation", retry_delay=0.1)

    # Check that there was a delay between attempts
    assert len(call_times) == 2
    time_diff = call_times[1] - call_times[0]
    assert time_diff >= 0.1


def test_execute_with_timeout_success(network_handler: NetworkErrorHandler) -> None:
    """Test timeout execution with successful operation."""

    def operation() -> str:
        return "success"

    result = network_handler.execute_with_timeout(operation, "test_operation")

    assert result == "success"


def test_execute_with_timeout_raises_timeout(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test timeout execution when operation times out."""

    def operation() -> str:
        raise TimeoutError("Operation timed out")

    with pytest.raises(TimeoutError):
        network_handler.execute_with_timeout(operation, "test_operation")


def test_check_upstream_availability(network_handler: NetworkErrorHandler) -> None:
    """Test upstream availability check."""
    target_url = "https://flow.ciandt.com/flow-llm-proxy"

    # Currently returns True as placeholder
    result = network_handler.check_upstream_availability(target_url)

    assert result is True


def test_get_retry_config(network_handler: NetworkErrorHandler) -> None:
    """Test getting retry configuration."""
    config = network_handler.get_retry_config()

    assert config["max_retries"] == 2
    assert config["retry_delay"] == 0.1
    assert config["default_timeout"] == 5.0


def test_update_retry_config(network_handler: NetworkErrorHandler) -> None:
    """Test updating retry configuration."""
    network_handler.update_retry_config(
        max_retries=5, retry_delay=0.5, default_timeout=10.0
    )

    assert network_handler.max_retries == 5
    assert network_handler.retry_delay == 0.5
    assert network_handler.default_timeout == 10.0


def test_update_retry_config_partial(network_handler: NetworkErrorHandler) -> None:
    """Test updating only some retry configuration values."""
    original_delay = network_handler.retry_delay

    network_handler.update_retry_config(max_retries=10)

    assert network_handler.max_retries == 10
    assert network_handler.retry_delay == original_delay  # unchanged


def test_network_handler_with_zero_retries() -> None:
    """Test network handler with zero retries."""
    handler = NetworkErrorHandler(max_retries=0)
    call_count = 0

    def operation() -> str:
        nonlocal call_count
        call_count += 1
        raise ConnectionError("Failure")

    with pytest.raises(ConnectionError):
        handler.execute_with_retry(operation, "test_operation")

    # Should only try once (no retries)
    assert call_count == 1


def test_execute_with_retry_no_sleep_on_last_attempt(
    network_handler: NetworkErrorHandler,
) -> None:
    """Test that retry logic doesn't sleep after the last failed attempt."""
    call_times = []

    def operation() -> str:
        call_times.append(time.time())
        raise ConnectionError("Failure")

    start_time = time.time()

    with pytest.raises(ConnectionError):
        network_handler.execute_with_retry(
            operation, "test_operation", max_retries=2, retry_delay=0.2
        )

    total_time = time.time() - start_time

    # Should have 3 attempts with 2 delays (0.2s each)
    # Total time should be around 0.4s, not 0.6s (which would include delay after last attempt)
    assert total_time < 0.5  # Allow some margin for execution time
