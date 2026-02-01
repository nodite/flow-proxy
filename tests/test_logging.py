"""Tests for logging utilities."""

import logging
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from flow_proxy_plugin.utils.logging import (
    CleanupConfig,
    ColoredFormatter,
    Colors,
    FormatConfig,
    LogConfig,
    LoggerFactory,
    LogSetup,
    RotationConfig,
    setup_colored_logger,
    setup_logging,
)


class TestColors:
    """Tests for Colors class."""

    def test_colors_defined(self) -> None:
        """Test that all color constants are defined."""
        assert Colors.RESET == "\033[0m"
        assert Colors.BOLD == "\033[1m"
        assert Colors.BRIGHT_BLACK == "\033[90m"
        assert Colors.BRIGHT_CYAN == "\033[96m"
        assert Colors.BRIGHT_YELLOW == "\033[93m"
        assert Colors.BRIGHT_RED == "\033[91m"
        assert Colors.RED == "\033[31m"


class TestColoredFormatter:
    """Tests for ColoredFormatter class."""

    def test_format_with_colors(self) -> None:
        """Test that formatter adds colors to log records."""
        formatter = ColoredFormatter(fmt="%(levelname)s %(name)s - %(message)s")
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)

        assert Colors.BRIGHT_CYAN in formatted  # INFO color
        assert Colors.BRIGHT_BLACK in formatted  # logger name color
        assert Colors.RESET in formatted
        assert "Test message" in formatted

    def test_format_different_levels(self) -> None:
        """Test formatting with different log levels."""
        formatter = ColoredFormatter(fmt="%(levelname)s")

        levels = [
            (logging.DEBUG, Colors.BRIGHT_BLACK),
            (logging.INFO, Colors.BRIGHT_CYAN),
            (logging.WARNING, Colors.BRIGHT_YELLOW),
            (logging.ERROR, Colors.BRIGHT_RED),
            (logging.CRITICAL, Colors.RED + Colors.BOLD),
        ]

        for level, expected_color in levels:
            record = logging.LogRecord(
                name="test",
                level=level,
                pathname="test.py",
                lineno=1,
                msg="Test",
                args=(),
                exc_info=None,
            )
            formatted = formatter.format(record)
            assert expected_color in formatted


class TestFormatConfig:
    """Tests for FormatConfig class."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = FormatConfig()

        assert config.console_format == "%(levelname)s %(name)s - %(message)s"
        assert config.console_date_format == "%H:%M:%S"
        assert "%(asctime)s" in config.file_format
        assert config.file_date_format == "%Y-%m-%d %H:%M:%S"

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = FormatConfig(
            console_format="%(message)s",
            console_date_format="%H:%M",
            file_format="%(levelname)s - %(message)s",
            file_date_format="%Y-%m-%d",
        )

        assert config.console_format == "%(message)s"
        assert config.console_date_format == "%H:%M"
        assert config.file_format == "%(levelname)s - %(message)s"
        assert config.file_date_format == "%Y-%m-%d"


class TestRotationConfig:
    """Tests for RotationConfig class."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = RotationConfig()

        assert config.when == "midnight"
        assert config.interval == 1
        assert config.backup_count == 0
        assert config.suffix == "%Y-%m-%d"
        assert config.encoding == "utf-8"

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = RotationConfig(
            when="H",
            interval=6,
            backup_count=10,
            suffix="%Y%m%d-%H%M%S",
            encoding="utf-16",
        )

        assert config.when == "H"
        assert config.interval == 6
        assert config.backup_count == 10
        assert config.suffix == "%Y%m%d-%H%M%S"
        assert config.encoding == "utf-16"


class TestCleanupConfig:
    """Tests for CleanupConfig class."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = CleanupConfig()

        assert config.enabled is True
        assert config.retention_days == 7
        assert config.cleanup_interval_hours == 24
        assert config.max_size_mb == 100

    def test_from_env_default(self) -> None:
        """Test creating config from environment with defaults."""
        with patch.dict("os.environ", {}, clear=True):
            config = CleanupConfig.from_env()

            assert config.enabled is True
            assert config.retention_days == 7
            assert config.cleanup_interval_hours == 24
            assert config.max_size_mb == 100

    def test_from_env_custom(self) -> None:
        """Test creating config from environment with custom values."""
        env = {
            "FLOW_PROXY_LOG_CLEANUP_ENABLED": "false",
            "FLOW_PROXY_LOG_RETENTION_DAYS": "30",
            "FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS": "12",
            "FLOW_PROXY_LOG_MAX_SIZE_MB": "500",
        }

        with patch.dict("os.environ", env, clear=True):
            config = CleanupConfig.from_env()

            assert config.enabled is False
            assert config.retention_days == 30
            assert config.cleanup_interval_hours == 12
            assert config.max_size_mb == 500


class TestLogConfig:
    """Tests for LogConfig class."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = LogConfig()

        assert config.level == "INFO"
        assert config.log_dir == "logs"
        assert config.log_filename == "flow_proxy_plugin.log"
        assert isinstance(config.format, FormatConfig)
        assert isinstance(config.rotation, RotationConfig)
        assert isinstance(config.cleanup, CleanupConfig)

    def test_log_level_property(self) -> None:
        """Test log_level property returns correct integer."""
        config = LogConfig(level="DEBUG")
        assert config.log_level == logging.DEBUG

        config = LogConfig(level="INFO")
        assert config.log_level == logging.INFO

        config = LogConfig(level="WARNING")
        assert config.log_level == logging.WARNING

        config = LogConfig(level="ERROR")
        assert config.log_level == logging.ERROR

    def test_log_dir_path_property(self) -> None:
        """Test log_dir_path property returns Path object."""
        config = LogConfig(log_dir="test_logs")
        assert isinstance(config.log_dir_path, Path)
        assert str(config.log_dir_path) == "test_logs"

    def test_log_file_path_property(self) -> None:
        """Test log_file_path property returns correct path."""
        config = LogConfig(log_dir="test_logs", log_filename="test.log")
        assert isinstance(config.log_file_path, Path)
        assert str(config.log_file_path) == "test_logs/test.log"

    def test_from_env_default(self) -> None:
        """Test creating config from environment with defaults."""
        with patch.dict("os.environ", {}, clear=True):
            config = LogConfig.from_env(level="DEBUG", log_dir="custom_logs")

            assert config.level == "DEBUG"
            assert config.log_dir == "custom_logs"
            assert config.cleanup.enabled is True
            assert config.cleanup.retention_days == 7

    def test_from_env_custom(self) -> None:
        """Test creating config from environment with custom values."""
        env = {
            "FLOW_PROXY_LOG_CLEANUP_ENABLED": "true",
            "FLOW_PROXY_LOG_RETENTION_DAYS": "14",
            "FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS": "6",
            "FLOW_PROXY_LOG_MAX_SIZE_MB": "200",
        }

        with patch.dict("os.environ", env, clear=True):
            config = LogConfig.from_env()

            assert config.cleanup.enabled is True
            assert config.cleanup.retention_days == 14
            assert config.cleanup.cleanup_interval_hours == 6
            assert config.cleanup.max_size_mb == 200

    def test_nested_config_initialization(self) -> None:
        """Test that nested configs are initialized properly."""
        config = LogConfig()

        assert config.format is not None
        assert config.rotation is not None
        assert config.cleanup is not None


class TestLoggerFactory:
    """Tests for LoggerFactory class."""

    def test_create_console_handler(self) -> None:
        """Test creating console handler."""
        config = LogConfig()
        handler = LoggerFactory.create_console_handler(config)

        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream == sys.stdout
        assert isinstance(handler.formatter, ColoredFormatter)

    def test_create_file_handler(self, tmp_path: Path) -> None:
        """Test creating file handler."""
        config = LogConfig(log_dir=str(tmp_path))
        handler = LoggerFactory.create_file_handler(config)

        assert handler.baseFilename == str(tmp_path / "flow_proxy_plugin.log")
        # TimedRotatingFileHandler converts 'when' to uppercase
        assert handler.when == "MIDNIGHT"
        # For 'midnight', interval is converted to seconds in a day
        assert handler.interval == 86400
        assert handler.backupCount == 0
        assert handler.suffix == "%Y-%m-%d"
        assert isinstance(handler.formatter, logging.Formatter)

    def test_file_handler_custom_config(self, tmp_path: Path) -> None:
        """Test creating file handler with custom configuration."""
        rotation = RotationConfig(when="H", interval=6, backup_count=5, suffix="%Y%m%d")
        config = LogConfig(log_dir=str(tmp_path), rotation=rotation)
        handler = LoggerFactory.create_file_handler(config)

        assert handler.when == "H"
        # TimedRotatingFileHandler converts interval to seconds
        # For 'H' (hours), 6 hours = 21600 seconds
        assert handler.interval == 21600
        assert handler.backupCount == 5
        assert handler.suffix == "%Y%m%d"


class TestLogSetup:
    """Tests for LogSetup class."""

    def test_init_creates_log_directory(self, tmp_path: Path) -> None:
        """Test that initialization creates log directory."""
        log_dir = tmp_path / "test_logs"
        assert not log_dir.exists()

        config = LogConfig(log_dir=str(log_dir))
        LogSetup(config)

        assert log_dir.exists()
        assert log_dir.is_dir()

    @patch("flow_proxy_plugin.utils.log_cleaner.init_log_cleaner")
    def test_initialize_cleaner(self, mock_init: Mock, tmp_path: Path) -> None:
        """Test initializing log cleaner."""
        config = LogConfig(log_dir=str(tmp_path))
        setup = LogSetup(config)
        setup.initialize_cleaner()

        mock_init.assert_called_once_with(
            log_dir=config.log_dir_path,
            retention_days=config.cleanup.retention_days,
            cleanup_interval_hours=config.cleanup.cleanup_interval_hours,
            max_size_mb=config.cleanup.max_size_mb,
            enabled=config.cleanup.enabled,
        )

    def test_configure_root_logger(self, tmp_path: Path) -> None:
        """Test configuring root logger."""
        config = LogConfig(log_dir=str(tmp_path), level="DEBUG")
        setup = LogSetup(config)

        # Clear any existing handlers
        logging.root.handlers.clear()

        setup.configure_root_logger()

        assert logging.root.level == logging.DEBUG
        assert len(logging.root.handlers) == 2  # console + file

    @patch("flow_proxy_plugin.utils.log_cleaner.init_log_cleaner")
    def test_setup_complete(self, mock_init: Mock, tmp_path: Path) -> None:
        """Test complete setup process."""
        config = LogConfig(log_dir=str(tmp_path))
        setup = LogSetup(config)

        # Clear any existing handlers
        logging.root.handlers.clear()

        setup.setup()

        # Check logger is configured
        assert len(logging.root.handlers) == 2

        # Check cleaner is initialized
        mock_init.assert_called_once()


class TestSetupLogging:
    """Tests for setup_logging function."""

    @patch("flow_proxy_plugin.utils.log_cleaner.init_log_cleaner")
    def test_setup_logging_default(self, mock_init: Mock, tmp_path: Path) -> None:
        """Test setup_logging with default parameters."""
        # Clear any existing handlers
        logging.root.handlers.clear()

        with patch.dict("os.environ", {}, clear=True):
            setup_logging(level="INFO", log_dir=str(tmp_path))

        assert logging.root.level == logging.INFO
        assert len(logging.root.handlers) == 2
        mock_init.assert_called_once()

    @patch("flow_proxy_plugin.utils.log_cleaner.init_log_cleaner")
    def test_setup_logging_custom_level(self, mock_init: Mock, tmp_path: Path) -> None:
        """Test setup_logging with custom log level."""
        logging.root.handlers.clear()

        with patch.dict("os.environ", {}, clear=True):
            setup_logging(level="DEBUG", log_dir=str(tmp_path))

        assert logging.root.level == logging.DEBUG


class TestSetupColoredLogger:
    """Tests for setup_colored_logger function."""

    def test_setup_colored_logger_default(self) -> None:
        """Test setup_colored_logger with default parameters."""
        logger = logging.getLogger("test.logger")
        setup_colored_logger(logger)

        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.StreamHandler)
        assert isinstance(logger.handlers[0].formatter, ColoredFormatter)
        assert logger.propagate is False

    def test_setup_colored_logger_custom_level(self) -> None:
        """Test setup_colored_logger with custom log level."""
        logger = logging.getLogger("test.custom.logger")
        setup_colored_logger(logger, log_level="DEBUG")

        assert logger.level == logging.DEBUG

    def test_setup_colored_logger_with_propagate(self) -> None:
        """Test setup_colored_logger with propagate enabled."""
        logger = logging.getLogger("test.propagate.logger")
        setup_colored_logger(logger, propagate=True)

        assert logger.propagate is True

    def test_setup_colored_logger_clears_handlers(self) -> None:
        """Test that setup_colored_logger clears existing handlers."""
        logger = logging.getLogger("test.clear.logger")

        # Add some handlers
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(logging.StreamHandler())
        assert len(logger.handlers) == 2

        # Setup should clear existing handlers
        setup_colored_logger(logger)

        assert len(logger.handlers) == 1
