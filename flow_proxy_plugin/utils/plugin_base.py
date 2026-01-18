"""Base utilities for plugins."""

import logging
import os
import threading
from typing import Optional

from ..core.config import SecretsManager
from ..core.jwt_generator import JWTGenerator
from ..core.load_balancer import LoadBalancer
from ..core.request_forwarder import RequestForwarder


class SharedComponentManager:
    """Thread-safe singleton manager for shared plugin components.

    This class manages shared components (LoadBalancer, configs) across all plugin
    instances using a thread-safe singleton pattern with lazy initialization.
    """

    _instance: Optional["SharedComponentManager"] = None
    _lock = threading.Lock()
    _initialized: bool

    def __new__(cls) -> "SharedComponentManager":
        """Create or return the singleton instance (thread-safe)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initialize the manager (only once)."""
        if hasattr(self, "_initialized") and self._initialized:
            return

        with self._lock:
            if hasattr(self, "_initialized") and self._initialized:
                return

            self._secrets_manager: SecretsManager | None = None
            self._configs: list | None = None
            self._load_balancer: LoadBalancer | None = None
            self._initialized: bool = True

    def get_or_create_components(
        self, logger: logging.Logger
    ) -> tuple[SecretsManager, list, LoadBalancer]:
        """Get or create shared components (thread-safe).

        Args:
            logger: Logger instance

        Returns:
            Tuple of (secrets_manager, configs, load_balancer)

        Raises:
            FileNotFoundError: If secrets.json file is not found
            ValueError: If configuration is invalid or empty
        """
        # Fast path: components already initialized
        if (
            self._load_balancer is not None
            and self._configs is not None
            and self._secrets_manager is not None
        ):
            logger.debug("Reusing existing shared plugin components")
            return self._secrets_manager, self._configs, self._load_balancer

        # Slow path: initialize components with lock
        with self._lock:
            # Double-check after acquiring lock
            if (
                self._load_balancer is not None
                and self._configs is not None
                and self._secrets_manager is not None
            ):
                return self._secrets_manager, self._configs, self._load_balancer

            logger.info("Initializing shared plugin components for the first time")

            # Initialize components
            self._secrets_manager = SecretsManager()
            secrets_file = os.getenv("FLOW_PROXY_SECRETS_FILE", "secrets.json")
            self._configs = self._secrets_manager.load_secrets(secrets_file)
            self._load_balancer = LoadBalancer(self._configs, logger)

            logger.info(
                "Shared LoadBalancer initialized with %d configs and will be "
                "reused across plugin instances",
                len(self._configs),
            )

            return self._secrets_manager, self._configs, self._load_balancer

    def reset(self) -> None:
        """Reset all shared components (primarily for testing)."""
        with self._lock:
            self._secrets_manager = None
            self._configs = None
            self._load_balancer = None


def initialize_plugin_components(
    logger: logging.Logger,
) -> tuple[SecretsManager, list, LoadBalancer, JWTGenerator, RequestForwarder]:
    """Initialize common plugin components with shared state.

    This function uses a singleton manager to ensure LoadBalancer and configurations
    are shared across all plugin instances, maintaining consistent round-robin state.
    The initialization is thread-safe for concurrent usage.

    Args:
        logger: Logger instance

    Returns:
        Tuple of (secrets_manager, configs, load_balancer, jwt_generator, request_forwarder)

    Raises:
        FileNotFoundError: If secrets.json file is not found
        ValueError: If configuration is invalid or empty
    """
    # Get shared components from singleton manager
    manager = SharedComponentManager()
    secrets_manager, configs, load_balancer = manager.get_or_create_components(logger)

    # Create new instances for components that don't need shared state
    jwt_generator = JWTGenerator(logger)
    request_forwarder = RequestForwarder(logger)

    return secrets_manager, configs, load_balancer, jwt_generator, request_forwarder
