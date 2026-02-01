"""Logging utilities with colored output."""

import logging
import sys


# ANSI color codes
class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"

    # Bright foreground colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_RED = "\033[91m"
    RED = "\033[31m"


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels."""

    LEVEL_COLORS = {
        logging.DEBUG: Colors.BRIGHT_BLACK,
        logging.INFO: Colors.BRIGHT_CYAN,
        logging.WARNING: Colors.BRIGHT_YELLOW,
        logging.ERROR: Colors.BRIGHT_RED,
        logging.CRITICAL: Colors.RED + Colors.BOLD,
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with colors."""
        # Add color to level name
        level_color = self.LEVEL_COLORS.get(record.levelno, "")
        record.levelname = f"{level_color}{record.levelname:8s}{Colors.RESET}"

        # Color the logger name
        record.name = f"{Colors.BRIGHT_BLACK}{record.name}{Colors.RESET}"

        # Format the message
        return super().format(record)


def setup_colored_logger(
    logger: logging.Logger, log_level: str = "INFO", propagate: bool = False
) -> None:
    """Setup colored logger for a plugin.

    Args:
        logger: Logger instance to configure
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR)
        propagate: Whether to propagate logs to parent loggers (default: False)
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Clear existing handlers
    if logger.handlers:
        logger.handlers.clear()

    # Add colored console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)  # Set handler level too
    console_handler.setFormatter(
        ColoredFormatter(fmt="%(levelname)s %(name)s - %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(console_handler)

    # Add file handler (get log file from environment)
    import os

    log_file = os.getenv("FLOW_PROXY_LOG_FILE", "flow_proxy_plugin.log")
    try:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s - pid:%(process)d - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)
    except Exception as e:
        # If file handler fails, just log to console
        logger.warning(f"Could not setup file handler: {e}")

    logger.propagate = propagate  # Allow control of propagation


def setup_logging(level: str = "INFO", log_file: str = "flow_proxy_plugin.log") -> None:
    """Setup logging configuration for the application.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Path to log file
    """
    import os
    from pathlib import Path

    # Ensure log directory exists
    log_path = Path(log_file)
    log_dir = log_path.parent if log_path.parent != Path('.') else Path('logs')
    log_dir.mkdir(parents=True, exist_ok=True)

    # If log_file is just a filename, put it in logs directory
    if log_path.parent == Path('.'):
        log_file = str(log_dir / log_path.name)

    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColoredFormatter(fmt="%(levelname)s %(name)s - %(message)s", datefmt="%H:%M:%S")
    )

    # File handler without colors
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        handlers=[console_handler, file_handler],
    )

    # Initialize log cleaner
    from .log_cleaner import init_log_cleaner

    # Get cleanup settings from environment
    cleanup_enabled = os.getenv("FLOW_PROXY_LOG_CLEANUP_ENABLED", "true").lower() == "true"
    retention_days = int(os.getenv("FLOW_PROXY_LOG_RETENTION_DAYS", "7"))
    cleanup_interval = int(os.getenv("FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS", "24"))
    max_size_mb = int(os.getenv("FLOW_PROXY_LOG_MAX_SIZE_MB", "100"))

    # Initialize the log cleaner
    init_log_cleaner(
        log_dir=log_dir,
        retention_days=retention_days,
        cleanup_interval_hours=cleanup_interval,
        max_size_mb=max_size_mb,
        enabled=cleanup_enabled,
    )
