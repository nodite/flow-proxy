"""Base plugin class with common functionality."""

import logging
from typing import Any

from ..utils.logging import setup_colored_logger
from ..utils.plugin_base import initialize_plugin_components


class BaseFlowProxyPlugin:
    """Base class for Flow Proxy plugins with common initialization."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize base plugin."""
        # This will be called by child classes after super().__init__()
        pass

    def _setup_logger(self, logger_name: str) -> logging.Logger:
        """Setup logger with colored output.

        Args:
            logger_name: Name for the logger

        Returns:
            Configured logger instance
        """
        logger = logging.getLogger(logger_name)

        # Set log level from command line flags if available
        if hasattr(self, "flags") and hasattr(self.flags, "log_level"):
            setup_colored_logger(logger, self.flags.log_level)

        return logger

    def _initialize_components(self, logger: logging.Logger) -> None:
        """Initialize common plugin components.

        Args:
            logger: Logger instance
        """
        (
            self.secrets_manager,
            self.configs,
            self.load_balancer,
            self.jwt_generator,
            self.request_forwarder,
        ) = initialize_plugin_components(logger)
