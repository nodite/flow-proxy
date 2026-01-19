"""Tests for log filter functionality."""

import logging
from unittest.mock import Mock

from flow_proxy_plugin.utils.log_filter import (
    BrokenPipeFilter,
    ProxyNoiseFilter,
    setup_proxy_log_filters,
)


class TestBrokenPipeFilter:
    """Test BrokenPipeFilter class."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.filter = BrokenPipeFilter()

    def test_filter_broken_pipe_error_warning(self) -> None:
        """Test that BrokenPipeError warnings are filtered out."""
        record = logging.LogRecord(
            name="proxy.http.handler",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="BrokenPipeError when flushing buffer for client",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is False

    def test_filter_connection_reset_error_warning(self) -> None:
        """Test that ConnectionResetError warnings are filtered out."""
        record = logging.LogRecord(
            name="proxy.http.handler",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="ConnectionResetError when sending data",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is False

    def test_allow_other_warnings(self) -> None:
        """Test that other warnings are not filtered."""
        record = logging.LogRecord(
            name="proxy.http.handler",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="Some other warning message",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is True

    def test_allow_errors(self) -> None:
        """Test that error level logs are not filtered."""
        record = logging.LogRecord(
            name="proxy.http.handler",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="BrokenPipeError when flushing buffer for client",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is True

    def test_allow_different_logger(self) -> None:
        """Test that logs from different loggers are not filtered."""
        record = logging.LogRecord(
            name="other.logger",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="BrokenPipeError when flushing buffer for client",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is True


class TestProxyNoiseFilter:
    """Test ProxyNoiseFilter class."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.filter = ProxyNoiseFilter()

    def test_filter_proxy_web_info_logs(self) -> None:
        """Test that proxy.http.server.web INFO logs are filtered out."""
        record = logging.LogRecord(
            name="proxy.http.server.web",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="127.0.0.1:56798 - GET / - curl/8.14.1 - 11775.52ms",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is False

    def test_allow_proxy_web_warnings(self) -> None:
        """Test that proxy.http.server.web warnings are not filtered."""
        record = logging.LogRecord(
            name="proxy.http.server.web",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="Some warning",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is True

    def test_allow_other_loggers_info(self) -> None:
        """Test that INFO logs from other loggers are not filtered."""
        record = logging.LogRecord(
            name="flow_proxy_plugin",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Plugin initialized",
            args=(),
            exc_info=None,
        )
        assert self.filter.filter(record) is True


class TestSetupProxyLogFilters:
    """Test setup_proxy_log_filters function."""

    def test_setup_broken_pipe_filter_only(self) -> None:
        """Test setting up only BrokenPipeFilter."""
        # Get logger and clear existing filters
        handler_logger = logging.getLogger("proxy.http.handler")
        handler_logger.filters.clear()

        # Apply filter
        setup_proxy_log_filters(suppress_broken_pipe=True, suppress_proxy_noise=False)

        # Check filter was added
        assert len(handler_logger.filters) == 1
        assert isinstance(handler_logger.filters[0], BrokenPipeFilter)

    def test_setup_proxy_noise_filter_only(self) -> None:
        """Test setting up only ProxyNoiseFilter."""
        # Get logger and clear existing filters
        web_logger = logging.getLogger("proxy.http.server.web")
        web_logger.filters.clear()

        # Apply filter
        setup_proxy_log_filters(suppress_broken_pipe=False, suppress_proxy_noise=True)

        # Check filter was added
        assert len(web_logger.filters) == 1
        assert isinstance(web_logger.filters[0], ProxyNoiseFilter)

    def test_setup_both_filters(self) -> None:
        """Test setting up both filters."""
        # Get loggers and clear existing filters
        handler_logger = logging.getLogger("proxy.http.handler")
        web_logger = logging.getLogger("proxy.http.server.web")
        handler_logger.filters.clear()
        web_logger.filters.clear()

        # Apply filters
        setup_proxy_log_filters(suppress_broken_pipe=True, suppress_proxy_noise=True)

        # Check both filters were added
        assert len(handler_logger.filters) == 1
        assert isinstance(handler_logger.filters[0], BrokenPipeFilter)
        assert len(web_logger.filters) == 1
        assert isinstance(web_logger.filters[0], ProxyNoiseFilter)

    def test_setup_no_filters(self) -> None:
        """Test that no filters are added when both options are False."""
        # Get loggers and clear existing filters
        handler_logger = logging.getLogger("proxy.http.handler")
        web_logger = logging.getLogger("proxy.http.server.web")
        initial_handler_filters = len(handler_logger.filters)
        initial_web_filters = len(web_logger.filters)

        # Apply with both False
        setup_proxy_log_filters(suppress_broken_pipe=False, suppress_proxy_noise=False)

        # Check no new filters were added
        assert len(handler_logger.filters) == initial_handler_filters
        assert len(web_logger.filters) == initial_web_filters


class TestIntegration:
    """Integration tests for log filtering."""

    def test_broken_pipe_filter_integration(self) -> None:
        """Test BrokenPipeFilter in a real logging scenario."""
        # Set up logger with filter
        logger = logging.getLogger("proxy.http.handler")
        logger.setLevel(logging.DEBUG)
        logger.filters.clear()

        # Add handler to capture logs
        handler = logging.Handler()
        handler.setLevel(logging.DEBUG)
        mock_emit = Mock()
        handler.emit = mock_emit  # type: ignore[method-assign]
        logger.addHandler(handler)

        # Add filter
        logger.addFilter(BrokenPipeFilter())

        # Log a BrokenPipeError warning (should be filtered)
        logger.warning("BrokenPipeError when flushing buffer for client")
        assert mock_emit.call_count == 0

        # Log a regular warning (should not be filtered)
        logger.warning("Regular warning message")
        assert mock_emit.call_count == 1

        # Clean up
        logger.removeHandler(handler)
        logger.filters.clear()

    def test_proxy_noise_filter_integration(self) -> None:
        """Test ProxyNoiseFilter in a real logging scenario."""
        # Set up logger with filter
        logger = logging.getLogger("proxy.http.server.web")
        logger.setLevel(logging.DEBUG)
        logger.filters.clear()

        # Add handler to capture logs
        handler = logging.Handler()
        handler.setLevel(logging.DEBUG)
        mock_emit = Mock()
        handler.emit = mock_emit  # type: ignore[method-assign]
        logger.addHandler(handler)

        # Add filter
        logger.addFilter(ProxyNoiseFilter())

        # Log an INFO message (should be filtered)
        logger.info("127.0.0.1:56798 - GET / - curl/8.14.1 - 11775.52ms")
        assert mock_emit.call_count == 0

        # Log a warning (should not be filtered)
        logger.warning("Something went wrong")
        assert mock_emit.call_count == 1

        # Clean up
        logger.removeHandler(handler)
        logger.filters.clear()
