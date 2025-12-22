"""Tests for RequestForwarder component."""

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from proxy.http.parser import HttpParser

from flow_proxy_plugin.core.request_forwarder import RequestForwarder


@pytest.fixture
def request_forwarder() -> RequestForwarder:
    """Create a RequestForwarder instance for testing."""
    logger = logging.getLogger("test")
    return RequestForwarder(logger)


@pytest.fixture
def mock_request() -> Any:
    """Create a mock HTTP request."""
    request = MagicMock(spec=HttpParser)
    request.headers = {}
    request.method = b"GET"
    request.path = b"/v1/models"
    return request


def test_request_forwarder_initialization(request_forwarder: RequestForwarder) -> None:
    """Test RequestForwarder initialization."""
    assert request_forwarder.target_base_url == "https://flow.ciandt.com/flow-llm-proxy"
    assert request_forwarder.target_host == "flow.ciandt.com"


def test_modify_request_headers_adds_authorization(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test that modify_request_headers adds Authorization header."""
    jwt_token = "test_token_123"

    modified_request = request_forwarder.modify_request_headers(
        mock_request, jwt_token, "test_config"
    )

    assert modified_request is mock_request
    assert b"Authorization" in modified_request.headers
    auth_value = modified_request.headers[b"Authorization"]
    assert auth_value[0] == b"Bearer test_token_123"


def test_modify_request_headers_updates_host(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test that modify_request_headers updates Host header."""
    jwt_token = "test_token_123"

    modified_request = request_forwarder.modify_request_headers(mock_request, jwt_token)

    assert b"Host" in modified_request.headers
    host_value = modified_request.headers[b"Host"]
    assert host_value[0] == b"flow.ciandt.com"


def test_modify_request_headers_removes_existing_auth(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test that existing Authorization headers are removed."""
    mock_request.headers[b"authorization"] = (b"Bearer old_token", b"")
    jwt_token = "new_token"

    modified_request = request_forwarder.modify_request_headers(mock_request, jwt_token)

    # Should have new Authorization header
    assert b"Authorization" in modified_request.headers
    auth_value = modified_request.headers[b"Authorization"]
    assert auth_value[0] == b"Bearer new_token"
    # Old lowercase header should be removed
    assert b"authorization" not in modified_request.headers


def test_modify_request_headers_invalid_token(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test that empty token raises ValueError."""
    with pytest.raises(ValueError, match="jwt_token is empty"):
        request_forwarder.modify_request_headers(mock_request, "")


def test_modify_request_headers_none_request(
    request_forwarder: RequestForwarder,
) -> None:
    """Test that None request raises ValueError."""
    with pytest.raises(ValueError, match="request is None"):
        request_forwarder.modify_request_headers(None, "token")


def test_get_target_url_basic_path(request_forwarder: RequestForwarder) -> None:
    """Test get_target_url with basic path."""
    url = request_forwarder.get_target_url("/v1/models")
    assert url == "https://flow.ciandt.com/flow-llm-proxy/v1/models"


def test_get_target_url_without_leading_slash(
    request_forwarder: RequestForwarder,
) -> None:
    """Test get_target_url adds leading slash if missing."""
    url = request_forwarder.get_target_url("v1/chat/completions")
    assert url == "https://flow.ciandt.com/flow-llm-proxy/v1/chat/completions"


def test_get_target_url_empty_path(request_forwarder: RequestForwarder) -> None:
    """Test get_target_url with empty path raises ValueError."""
    with pytest.raises(ValueError, match="original_path is empty"):
        request_forwarder.get_target_url("")


def test_handle_response_chunk_transparent_passthrough(
    request_forwarder: RequestForwarder,
) -> None:
    """Test that response chunks are passed through unchanged."""
    test_data = b"test response data"
    chunk = memoryview(test_data)

    result = request_forwarder.handle_response_chunk(chunk)

    assert result == chunk
    assert bytes(result) == test_data


def test_handle_streaming_response(request_forwarder: RequestForwarder) -> None:
    """Test streaming response handling."""
    test_data = b"streaming data chunk"
    chunk = memoryview(test_data)

    result = request_forwarder.handle_streaming_response(chunk)

    assert result == chunk
    assert bytes(result) == test_data


def test_handle_forwarding_error_network(request_forwarder: RequestForwarder) -> None:
    """Test network error handling."""
    error = ConnectionError("Network unreachable")

    result = request_forwarder.handle_forwarding_error(error, "network")

    assert isinstance(result, memoryview)
    assert b"502" in bytes(result)


def test_handle_forwarding_error_timeout(request_forwarder: RequestForwarder) -> None:
    """Test timeout error handling."""
    error = TimeoutError("Request timeout")

    result = request_forwarder.handle_forwarding_error(error, "timeout")

    assert isinstance(result, memoryview)
    assert b"502" in bytes(result)


def test_handle_forwarding_error_invalid_request(
    request_forwarder: RequestForwarder,
) -> None:
    """Test invalid request error handling."""
    error = ValueError("Invalid request format")

    result = request_forwarder.handle_forwarding_error(error, "invalid_request")

    assert isinstance(result, memoryview)
    assert b"400" in bytes(result)


def test_handle_forwarding_error_general(request_forwarder: RequestForwarder) -> None:
    """Test general error handling."""
    error = Exception("Unexpected error")

    result = request_forwarder.handle_forwarding_error(error, "general")

    assert isinstance(result, memoryview)
    assert b"500" in bytes(result)


def test_validate_request_valid(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test request validation with valid request."""
    assert request_forwarder.validate_request(mock_request) is True


def test_validate_request_none(request_forwarder: RequestForwarder) -> None:
    """Test request validation with None request."""
    assert request_forwarder.validate_request(None) is False


def test_validate_request_missing_method(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test request validation with missing method."""
    mock_request.method = None
    assert request_forwarder.validate_request(mock_request) is False


def test_validate_request_missing_path(
    request_forwarder: RequestForwarder, mock_request: Any
) -> None:
    """Test request validation with missing path."""
    mock_request.path = None
    assert request_forwarder.validate_request(mock_request) is False
