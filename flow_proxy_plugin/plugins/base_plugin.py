"""Base plugin mixin providing shared services and utilities."""

from typing import Any

from ..utils.log_context import component_context
from ..utils.process_services import ProcessServices


class BaseFlowProxyPlugin:
    """Mixin for Flow Proxy plugins. Provides shared service references and utilities.

    Concrete plugins must call _init_services() once in their __init__ (when not pooled).
    They must override _rebind() to rebind connection-specific state on pool reuse.
    """

    _pooled: bool = False  # Class-level default; instance attr set to True after first init

    def _init_services(self) -> None:
        """Bind service references from ProcessServices. Call once in first __init__."""
        svc = ProcessServices.get()
        self.logger = svc.logger
        self.secrets_manager = svc.secrets_manager
        self.configs = svc.configs
        self.load_balancer = svc.load_balancer
        self.jwt_generator = svc.jwt_generator
        self.request_forwarder = svc.request_forwarder
        self.request_filter = svc.request_filter

    def _rebind(self, *args: Any, **kwargs: Any) -> None:
        """Rebind connection-specific state for pool reuse. Override in each subclass."""
        raise NotImplementedError

    def _reset_request_state(self) -> None:
        """Clear per-request state before returning instance to pool.

        Override in subclasses if handle_request() assigns any self.* request-scoped state.
        """

    def _get_config_and_token(self) -> tuple[dict[str, Any], str, str]:
        """Get next config and generate JWT token with failover support."""
        with component_context("LB"):
            config = self.load_balancer.get_next_config()
        config_name = config.get("name", config.get("clientId", "unknown"))
        try:
            with component_context("JWT"):
                jwt_token = self.jwt_generator.generate_token(config)
            return config, config_name, jwt_token
        except ValueError as e:
            self.logger.error(
                "Token generation failed for '%s': %s", config_name, str(e)
            )
            with component_context("LB"):
                self.load_balancer.mark_config_failed(config)
                config = self.load_balancer.get_next_config()
            config_name = config.get("name", config.get("clientId", "unknown"))
            with component_context("JWT"):
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
