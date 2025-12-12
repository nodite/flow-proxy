"""Load balancer implementation with round-robin strategy."""

import logging


class LoadBalancer:
    """Implements round-robin load balancing strategy."""

    def __init__(self, configs: list[dict[str, str]], logger: logging.Logger) -> None:
        """Initialize load balancer with authentication configurations and logger.

        Args:
            configs: List of authentication configurations
            logger: Logger instance for recording operations
        """
        self.configs = configs.copy()
        self.available_configs = configs.copy()
        self.failed_configs: list[dict[str, str]] = []
        self.current_index = 0
        self.logger = logger

        self.logger.info(f"LoadBalancer initialized with {len(configs)} configurations")

    def get_next_config(self) -> dict[str, str]:
        """Get next authentication configuration using round-robin strategy and log
        configuration name.

        Returns:
            Next available authentication configuration

        Raises:
            RuntimeError: If no configurations are available
        """
        if not self.available_configs:
            self.logger.error("No available configurations for load balancing")
            raise RuntimeError("No available configurations")

        # Get next config using round-robin
        config = self.available_configs[self.current_index]

        # Log configuration usage
        self.log_config_usage(config)

        # Move to next index, wrap around if necessary
        self.current_index = (self.current_index + 1) % len(self.available_configs)

        return config

    def mark_config_failed(self, config: dict[str, str]) -> None:
        """Mark configuration as failed, remove from available list and log.

        Args:
            config: Configuration that failed
        """
        if config in self.available_configs:
            self.available_configs.remove(config)
            self.failed_configs.append(config)

            config_name = config.get("name", "unknown")
            self.logger.warning(f"Configuration marked as failed: {config_name}")

            # Adjust current index if necessary
            if (
                self.current_index >= len(self.available_configs)
                and self.available_configs
            ):
                self.current_index = 0

    def reset_failed_configs(self) -> None:
        """Reset failed configurations, add back to available list."""
        if self.failed_configs:
            self.available_configs.extend(self.failed_configs)
            failed_count = len(self.failed_configs)
            self.failed_configs.clear()
            self.current_index = 0

            self.logger.info(
                f"Reset {failed_count} failed configurations back to available"
            )

    def log_config_usage(self, config: dict[str, str]) -> None:
        """Log the authentication configuration name being used.

        Args:
            config: Configuration being used
        """
        config_name = config.get("name", "unknown")
        self.logger.info(f"Using authentication configuration: {config_name}")
