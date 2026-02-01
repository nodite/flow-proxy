"""Base plugin class with shared functionality."""

import logging
import os
from typing import Any

from ..utils.log_filter import setup_proxy_log_filters
from ..utils.logging import setup_colored_logger
from ..utils.plugin_base import initialize_plugin_components


class BaseFlowProxyPlugin:
    """Base class for Flow Proxy plugins with shared initialization logic."""

    def _setup_logging(self) -> None:
        """Set up logging with colored output and filters."""
        self.logger = logging.getLogger(self.__class__.__name__)

        # Determine log level from environment or flags
        log_level = os.getenv("FLOW_PROXY_LOG_LEVEL", "INFO")

        if hasattr(self, "flags") and hasattr(self.flags, "log_level"):
            flags_level = getattr(self.flags, "log_level", None)
            if not os.getenv("FLOW_PROXY_LOG_LEVEL") and isinstance(flags_level, str):
                log_level = flags_level

        # Setup logger with console output and propagation to root logger (for file logging)
        setup_colored_logger(self.logger, log_level, propagate=True)
        setup_proxy_log_filters(suppress_broken_pipe=True, suppress_proxy_noise=True)

    def _initialize_components(self) -> None:
        """Initialize core components for request processing.

        Uses SharedComponentManager to maintain state across plugin instances.
        This is essential for LoadBalancer to work correctly in multi-threaded/multi-process mode.
        """
        try:
            # Use existing SharedComponentManager for thread-safe shared state
            (
                self.secrets_manager,
                self.configs,
                self.load_balancer,
                self.jwt_generator,
                self.request_forwarder,
            ) = initialize_plugin_components(self.logger)

            self.logger.info("✓ Plugin ready with %d configs", len(self.configs))

        except Exception as e:
            self.logger.critical("Failed to initialize: %s", str(e))
            raise

    def _get_config_and_token(self) -> tuple[dict[str, Any], str, str]:
        """Get next config and generate JWT token with failover support.

        Returns:
            Tuple of (config, config_name, jwt_token)

        Raises:
            RuntimeError: If no available configurations
            ValueError: If token generation fails for all configs
        """
        config = self.load_balancer.get_next_config()
        config_name = config.get("name", config.get("clientId", "unknown"))

        try:
            jwt_token = self.jwt_generator.generate_token(config)
            return config, config_name, jwt_token

        except ValueError as e:
            # Token generation failed, try failover
            self.logger.error(
                "Token generation failed for '%s': %s", config_name, str(e)
            )
            self.load_balancer.mark_config_failed(config)

            # Attempt failover
            config = self.load_balancer.get_next_config()
            config_name = config.get("name", config.get("clientId", "unknown"))
            jwt_token = self.jwt_generator.generate_token(config)

            self.logger.info("Failover successful - using '%s'", config_name)
            return config, config_name, jwt_token

    @staticmethod
    def _decode_bytes(value: bytes | str) -> str:
        """Safely decode bytes to string."""
        return value.decode() if isinstance(value, bytes) else value

    @staticmethod
    def _extract_header_value(header_value: Any) -> str:
        """Extract actual value from header tuple or bytes."""
        if isinstance(header_value, tuple):
            actual_value = header_value[0]
        else:
            actual_value = header_value

        return (
            actual_value.decode()
            if isinstance(actual_value, bytes)
            else str(actual_value)
        )
