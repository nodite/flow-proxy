"""Load balancer for distributing requests across multiple authentication configurations."""

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class LoadBalancerStats:
    """Statistics for load balancer operations."""

    total_requests: int = 0
    available_count: int = 0
    failed_count: int = 0
    total_count: int = 0


class LoadBalancer:
    """Thread-safe round-robin load balancer for authentication configurations.

    This class implements a round-robin load balancing strategy with automatic
    failover support. All operations are thread-safe through internal locking.

    Example:
        >>> lb = LoadBalancer(configs, logger)
        >>> config = lb.get_next_config()  # Thread-safe
        >>> lb.mark_config_failed(config)  # Automatic failover
    """

    def __init__(
        self, configs: list[dict[str, str]], logger: logging.Logger | None = None
    ) -> None:
        """Initialize load balancer with authentication configurations.

        Args:
            configs: List of authentication configurations
            logger: Optional logger instance

        Raises:
            ValueError: If configs list is empty
        """
        if not configs:
            raise ValueError("Cannot initialize LoadBalancer with empty configs list")

        self._logger = self._setup_logger(logger)
        self._lock = threading.Lock()

        # Configuration state
        self._all_configs = configs.copy()
        self._available_configs = configs.copy()
        self._failed_configs: list[dict[str, str]] = []

        # Round-robin state
        self._current_index = 0
        self._total_requests = 0

        self._logger.info(
            "LoadBalancer initialized with %d configurations", len(self._all_configs)
        )

    @staticmethod
    def _setup_logger(logger: logging.Logger | None) -> logging.Logger:
        """Setup and return logger instance."""
        if logger is not None:
            return logger

        logger = logging.getLogger(__name__)

        # Setup colored logging if needed
        if not logger.handlers:
            log_level = os.getenv("FLOW_PROXY_LOG_LEVEL", "INFO")
            if log_level:
                from ..utils.logging import setup_colored_logger

                setup_colored_logger(logger, log_level, propagate=True)

        return logger

    @contextmanager
    def _thread_safe(self) -> Iterator[None]:
        """Context manager for thread-safe operations."""
        with self._lock:
            yield

    def get_next_config(self) -> dict[str, str]:
        """Get next authentication configuration using round-robin strategy.

        This method is thread-safe and supports concurrent access.

        Returns:
            Next available authentication configuration

        Raises:
            RuntimeError: If no configurations are available

        Example:
            >>> config = lb.get_next_config()
            >>> print(config['name'])
        """
        with self._thread_safe():
            if not self._available_configs:
                raise RuntimeError(
                    "No available authentication configurations - all configs have failed"
                )

            # Round-robin selection
            config = self._available_configs[self._current_index]
            self._total_requests += 1

            # Log usage
            self._log_config_usage(config)

            # Advance to next index (circular)
            self._current_index = (self._current_index + 1) % len(
                self._available_configs
            )

            return config

    def mark_config_failed(self, config: dict[str, str]) -> None:
        """Mark configuration as failed and remove from available pool.

        Thread-safe operation that automatically handles failover.

        Args:
            config: The authentication configuration that failed

        Example:
            >>> try:
            ...     # Use config...
            ... except AuthError:
            ...     lb.mark_config_failed(config)
        """
        with self._thread_safe():
            if config not in self._available_configs:
                self._logger.warning(
                    "Config '%s' already marked as failed",
                    self._extract_config_name(config),
                )
                return

            # Move from available to failed
            self._available_configs.remove(config)
            self._failed_configs.append(config)

            # Log the failure
            self._logger.error(
                "Config '%s' marked as failed. Available: %d/%d",
                self._extract_config_name(config),
                len(self._available_configs),
                len(self._all_configs),
            )

            # Adjust index if needed
            self._adjust_current_index()

    def reset_failed_configs(self) -> None:
        """Reset all failed configurations back to available pool.

        Thread-safe operation useful for recovery scenarios.

        Example:
            >>> lb.reset_failed_configs()  # Restore all configs
        """
        with self._thread_safe():
            if not self._failed_configs:
                self._logger.debug("No failed configurations to reset")
                return

            count = len(self._failed_configs)
            self._available_configs.extend(self._failed_configs)
            self._failed_configs.clear()
            self._current_index = 0

            self._logger.info(
                "Reset %d failed configs. Total available: %d",
                count,
                len(self._available_configs),
            )

    def _adjust_current_index(self) -> None:
        """Adjust current index after configuration removal."""
        if self._available_configs:
            self._current_index %= len(self._available_configs)
        else:
            self._current_index = 0
            self._logger.critical("All authentication configurations have failed!")

    def _log_config_usage(self, config: dict[str, str]) -> None:
        """Log configuration usage with context."""
        self._logger.info(
            "Using config '%s' (request #%d, index %d)",
            self._extract_config_name(config),
            self._total_requests,
            self._current_index,
        )

    @staticmethod
    def _extract_config_name(config: dict[str, str]) -> str:
        """Extract human-readable name from configuration.

        Args:
            config: Configuration dictionary

        Returns:
            Configuration name or fallback identifier
        """
        # Priority: name > clientId prefix > object id
        if "name" in config:
            return config["name"]

        if "clientId" in config:
            return f"config-{config['clientId'][:8]}..."

        return f"config-{id(config)}"

    def get_stats(self) -> LoadBalancerStats:
        """Get current load balancer statistics.

        Returns:
            LoadBalancerStats with current statistics

        Example:
            >>> stats = lb.get_stats()
            >>> print(f"Processed {stats.total_requests} requests")
        """
        with self._thread_safe():
            return LoadBalancerStats(
                total_requests=self._total_requests,
                available_count=len(self._available_configs),
                failed_count=len(self._failed_configs),
                total_count=len(self._all_configs),
            )

    # Properties for backward compatibility
    @property
    def available_count(self) -> int:
        """Number of currently available configurations."""
        return len(self._available_configs)

    @property
    def failed_count(self) -> int:
        """Number of currently failed configurations."""
        return len(self._failed_configs)

    @property
    def total_count(self) -> int:
        """Total number of configurations."""
        return len(self._all_configs)

    @property
    def total_requests(self) -> int:
        """Total number of requests processed."""
        return self._total_requests

    def __repr__(self) -> str:
        """String representation of LoadBalancer."""
        stats = self.get_stats()
        return (
            f"LoadBalancer(total={stats.total_count}, "
            f"available={stats.available_count}, "
            f"failed={stats.failed_count}, "
            f"requests={stats.total_requests})"
        )
