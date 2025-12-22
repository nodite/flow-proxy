"""Base utilities for plugins."""

import logging

from ..core.config import SecretsManager
from ..core.jwt_generator import JWTGenerator
from ..core.load_balancer import LoadBalancer
from ..core.request_forwarder import RequestForwarder


def initialize_plugin_components(
    logger: logging.Logger,
) -> tuple[SecretsManager, list, LoadBalancer, JWTGenerator, RequestForwarder]:
    """Initialize common plugin components.

    Args:
        logger: Logger instance

    Returns:
        Tuple of (secrets_manager, configs, load_balancer, jwt_generator, request_forwarder)

    Raises:
        FileNotFoundError: If secrets.json file is not found
        ValueError: If configuration is invalid or empty
    """
    # Initialize SecretsManager and load configurations
    secrets_manager = SecretsManager()
    configs = secrets_manager.load_secrets("secrets.json")

    # Initialize LoadBalancer with loaded configurations
    load_balancer = LoadBalancer(configs, logger)

    # Initialize JWTGenerator for token generation
    jwt_generator = JWTGenerator(logger)

    # Initialize RequestForwarder for request handling
    request_forwarder = RequestForwarder(logger)

    return secrets_manager, configs, load_balancer, jwt_generator, request_forwarder
