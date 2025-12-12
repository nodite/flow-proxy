"""Load balancer for distributing requests across multiple authentication configurations."""

import logging
from typing import Any


class LoadBalancer:
    """Implements Round-robin load balancing strategy for authentication configurations."""

    def __init__(
        self, configs: list[dict[str, str]], logger: logging.Logger | None = None
    ) -> None:
        """Initialize load balancer with authentication configurations.

        Args:
            configs: List of authentication configurations
            logger: Optional logger instance (creates new one if not provided)

        Raises:
            ValueError: If configs list is empty
        """
        if not configs:
            raise ValueError("Cannot initialize LoadBalancer with empty configs list")

        self.logger = logger or logging.getLogger(__name__)
        self._all_configs = configs.copy()
        self._available_configs = configs.copy()
        self._failed_configs: list[dict[str, str]] = []
        self._current_index = 0
        self._total_requests = 0

        self.logger.info(
            f"LoadBalancer initialized with {len(self._all_configs)} authentication configurations"
        )

    def get_next_config(self) -> dict[str, str]:
        """Get next authentication configuration using Round-robin strategy.

        Returns:
            Next available authentication configuration

        Raises:
            RuntimeError: If no configurations are available
        """
        if not self._available_configs:
            error_msg = (
                "No available authentication configurations - all configs have failed"
            )
            self.logger.error(error_msg)
            raise RuntimeError(error_msg)

        # Get the next config using round-robin
        config = self._available_configs[self._current_index]
        self._total_requests += 1

        # Log the configuration being used
        self.log_config_usage(config)

        # Move to next index, wrapping around if necessary
        self._current_index = (self._current_index + 1) % len(self._available_configs)

        return config

    def mark_config_failed(self, config: dict[str, str]) -> None:
        """Mark configuration as failed and remove from available list.

        Args:
            config: The authentication configuration that failed
        """
        if config not in self._available_configs:
            self.logger.warning(
                f"Attempted to mark already-failed config as failed: {self._get_config_name(config)}"
            )
            return

        # Remove from available configs
        self._available_configs.remove(config)
        self._failed_configs.append(config)

        config_name = self._get_config_name(config)
        self.logger.error(
            f"Configuration '{config_name}' marked as failed. "
            f"Remaining available configs: {len(self._available_configs)}/{len(self._all_configs)}"
        )

        # Adjust current index if necessary
        if self._available_configs:
            self._current_index = self._current_index % len(self._available_configs)
        else:
            self._current_index = 0
            self.logger.critical("All authentication configurations have failed!")

    def reset_failed_configs(self) -> None:
        """Reset failed configurations and restore them to available list."""
        if not self._failed_configs:
            self.logger.debug("No failed configurations to reset")
            return

        failed_count = len(self._failed_configs)
        self._available_configs.extend(self._failed_configs)
        self._failed_configs.clear()
        self._current_index = 0

        self.logger.info(
            f"Reset {failed_count} failed configurations. "
            f"Total available configs: {len(self._available_configs)}"
        )

    def log_config_usage(self, config: dict[str, str]) -> None:
        """Log the authentication configuration name being used.

        Args:
            config: The authentication configuration being used
        """
        config_name = self._get_config_name(config)
        self.logger.info(
            f"Using authentication configuration: '{config_name}' "
            f"(request #{self._total_requests}, index {self._current_index})"
        )

    def _get_config_name(self, config: dict[str, str]) -> str:
        """Get the name of a configuration for logging purposes.

        Args:
            config: The authentication configuration

        Returns:
            Configuration name or a fallback identifier
        """
        # Try to get the 'name' field first
        if "name" in config:
            return config["name"]

        # Fallback to clientId if name is not available
        if "clientId" in config:
            return f"config-{config['clientId'][:8]}..."

        # Last resort: use object id
        return f"config-{id(config)}"

    @property
    def available_count(self) -> int:
        """Get the number of currently available configurations.

        Returns:
            Number of available configurations
        """
        return len(self._available_configs)

    @property
    def failed_count(self) -> int:
        """Get the number of currently failed configurations.

        Returns:
            Number of failed configurations
        """
        return len(self._failed_configs)

    @property
    def total_count(self) -> int:
        """Get the total number of configurations.

        Returns:
            Total number of configurations
        """
        return len(self._all_configs)

    @property
    def total_requests(self) -> int:
        """Get the total number of requests processed.

        Returns:
            Total number of requests
        """
        return self._total_requests
