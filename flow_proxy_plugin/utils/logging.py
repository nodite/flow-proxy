"""Logging utilities with colored output and daily rotation."""

import logging
import os
import sys
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import ClassVar


# ANSI color codes
class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_RED = "\033[91m"
    RED = "\033[31m"


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels."""

    LEVEL_COLORS: ClassVar[dict[int, str]] = {
        logging.DEBUG: Colors.BRIGHT_BLACK,
        logging.INFO: Colors.BRIGHT_CYAN,
        logging.WARNING: Colors.BRIGHT_YELLOW,
        logging.ERROR: Colors.BRIGHT_RED,
        logging.CRITICAL: Colors.RED + Colors.BOLD,
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors."""
        level_color = self.LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{level_color}{record.levelname:8s}{Colors.RESET}"
        record.name = f"{Colors.BRIGHT_BLACK}{record.name}{Colors.RESET}"
        return super().format(record)


@dataclass
class FormatConfig:
    """Log format configuration."""

    console_format: str = "%(levelname)s %(name)s - %(message)s"
    console_date_format: str = "%H:%M:%S"
    file_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file_date_format: str = "%Y-%m-%d %H:%M:%S"


@dataclass
class RotationConfig:
    """Log rotation configuration."""

    when: str = "midnight"
    interval: int = 1
    backup_count: int = 7  # 保留 7 天备份，防止日志无限增长
    suffix: str = "%Y-%m-%d"
    encoding: str = "utf-8"


@dataclass
class CleanupConfig:
    """Log cleanup configuration."""

    enabled: bool = True
    retention_days: int = 7
    cleanup_interval_hours: int = 24
    max_size_mb: int = 100

    @classmethod
    def from_env(cls) -> "CleanupConfig":
        """Create config from environment variables."""
        return cls(
            enabled=os.getenv("FLOW_PROXY_LOG_CLEANUP_ENABLED", "true").lower() == "true",
            retention_days=int(os.getenv("FLOW_PROXY_LOG_RETENTION_DAYS", "7")),
            cleanup_interval_hours=int(os.getenv("FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS", "24")),
            max_size_mb=int(os.getenv("FLOW_PROXY_LOG_MAX_SIZE_MB", "100")),
        )


@dataclass
class LogConfig:
    """Main log configuration."""

    level: str = "INFO"
    log_dir: str = "logs"
    log_filename: str = "flow_proxy_plugin.log"
    format: FormatConfig = None  # type: ignore
    rotation: RotationConfig = None  # type: ignore
    cleanup: CleanupConfig = None  # type: ignore

    def __post_init__(self) -> None:
        """Initialize nested configs with defaults if not provided."""
        if self.format is None:
            self.format = FormatConfig()
        if self.rotation is None:
            self.rotation = RotationConfig()
        if self.cleanup is None:
            self.cleanup = CleanupConfig.from_env()

    @classmethod
    def from_env(cls, level: str = "INFO", log_dir: str = "logs") -> "LogConfig":
        """Create config from environment variables."""
        return cls(
            level=level,
            log_dir=log_dir,
            format=FormatConfig(),
            rotation=RotationConfig(),
            cleanup=CleanupConfig.from_env(),
        )

    @property
    def log_level(self) -> int:
        """Get logging level as integer."""
        return getattr(logging, self.level.upper(), logging.INFO)

    @property
    def log_dir_path(self) -> Path:
        """Get log directory as Path object."""
        return Path(self.log_dir)

    @property
    def log_file_path(self) -> Path:
        """Get full log file path."""
        return self.log_dir_path / self.log_filename


class LoggerFactory:
    """Factory for creating logging handlers and formatters."""

    @staticmethod
    def create_console_handler(config: LogConfig) -> logging.StreamHandler:
        """Create console handler with colored formatter."""
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            ColoredFormatter(
                fmt=config.format.console_format,
                datefmt=config.format.console_date_format,
            )
        )
        return handler

    @staticmethod
    def create_file_handler(config: LogConfig) -> TimedRotatingFileHandler:
        """Create rotating file handler for daily logs."""
        handler = TimedRotatingFileHandler(
            filename=str(config.log_file_path),
            when=config.rotation.when,
            interval=config.rotation.interval,
            backupCount=config.rotation.backup_count,
            encoding=config.rotation.encoding,
        )
        handler.suffix = config.rotation.suffix
        handler.setFormatter(
            logging.Formatter(
                fmt=config.format.file_format,
                datefmt=config.format.file_date_format,
            )
        )
        return handler


class LogSetup:
    """Main logging setup coordinator."""

    def __init__(self, config: LogConfig):
        """Initialize with configuration."""
        self.config = config
        self._ensure_log_directory()

    def _ensure_log_directory(self) -> None:
        """Ensure log directory exists."""
        self.config.log_dir_path.mkdir(parents=True, exist_ok=True)

    def configure_root_logger(self) -> None:
        """Configure root logger with console and file handlers."""
        console_handler = LoggerFactory.create_console_handler(self.config)
        file_handler = LoggerFactory.create_file_handler(self.config)

        logging.basicConfig(
            level=self.config.log_level,
            handlers=[console_handler, file_handler],
            force=True,  # Override any existing configuration
        )

    def initialize_cleaner(self) -> None:
        """Initialize log cleaner for automatic cleanup."""
        from .log_cleaner import init_log_cleaner

        init_log_cleaner(
            log_dir=self.config.log_dir_path,
            retention_days=self.config.cleanup.retention_days,
            cleanup_interval_hours=self.config.cleanup.cleanup_interval_hours,
            max_size_mb=self.config.cleanup.max_size_mb,
            enabled=self.config.cleanup.enabled,
        )

    def setup(self) -> None:
        """Perform complete logging setup."""
        self.configure_root_logger()
        self.initialize_cleaner()


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Setup logging configuration for the application.

    This is the main entry point for logging configuration. It:
    - Creates a log configuration from environment variables
    - Sets up console output with colors
    - Configures daily rotating file logs
    - Initializes automatic log cleanup

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Log directory path

    Example:
        >>> setup_logging(level="INFO", log_dir="logs")
        # Creates logs/flow_proxy_plugin.log with daily rotation
        # Old logs: logs/flow_proxy_plugin.log.2026-02-01, etc.
    """
    config = LogConfig.from_env(level=level, log_dir=log_dir)
    setup = LogSetup(config)
    setup.setup()


def setup_colored_logger(
    logger: logging.Logger,
    log_level: str = "INFO",
    propagate: bool = False,
) -> None:
    """Setup colored console logger for a specific logger instance.

    This is useful for setting up individual module loggers with
    colored output, separate from the root logger configuration.

    Args:
        logger: Logger instance to configure
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR)
        propagate: Whether to propagate logs to parent loggers

    Example:
        >>> logger = logging.getLogger(__name__)
        >>> setup_colored_logger(logger, log_level="DEBUG")
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Clear existing handlers
    logger.handlers.clear()

    # Add colored console handler
    config = LogConfig(level=log_level)
    console_handler = LoggerFactory.create_console_handler(config)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    logger.propagate = propagate


def setup_file_handler_for_child_process(
    logger: logging.Logger,
    log_level: str = "INFO",
    log_dir: str = "logs",
) -> None:
    """Setup file handler for logger in child process.

    This function creates a NEW file handler in the child process,
    which is necessary because file handlers from the parent process
    don't work correctly after fork() due to file descriptor issues.

    Args:
        logger: Logger instance to add file handler to
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR)
        log_dir: Log directory path

    Example:
        >>> # In child process after fork
        >>> logger = logging.getLogger(__name__)
        >>> setup_file_handler_for_child_process(logger, "DEBUG", "logs")
    """
    # Create new config for child process
    config = LogConfig.from_env(level=log_level, log_dir=log_dir)

    # Ensure log directory exists
    config.log_dir_path.mkdir(parents=True, exist_ok=True)

    # Create NEW file handler (not copy from parent)
    file_handler = LoggerFactory.create_file_handler(config)
    file_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Add file handler to logger (avoid duplicates)
    for handler in logger.handlers:
        if isinstance(handler, TimedRotatingFileHandler):
            logger.removeHandler(handler)

    logger.addHandler(file_handler)
