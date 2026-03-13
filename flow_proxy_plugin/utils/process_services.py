"""Process-level singleton owning all shared plugin resources."""

import logging
import os
import threading
from typing import Optional

import httpx

from ..core.config import SecretsManager
from ..core.jwt_generator import JWTGenerator
from ..core.load_balancer import LoadBalancer
from ..core.request_forwarder import RequestForwarder
from ..plugins.request_filter import RequestFilter
from .log_filter import setup_proxy_log_filters
from .logging import setup_colored_logger, setup_file_handler_for_child_process


class ProcessServices:  # pylint: disable=too-many-instance-attributes
    """Process-level singleton. Lazily initialized after fork — fork-safe."""

    _instance: Optional["ProcessServices"] = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "ProcessServices":
        """Return singleton, creating it on first call (double-checked locking)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = object.__new__(cls)
                    instance._initialize()
                    cls._instance = instance
        return cls._instance

    def _initialize(self) -> None:
        """Initialize all process-level resources. Called exactly once per process."""
        log_level = os.getenv("FLOW_PROXY_LOG_LEVEL", "INFO")
        log_dir = os.getenv("FLOW_PROXY_LOG_DIR", "logs")

        self.logger = logging.getLogger("flow_proxy")
        setup_colored_logger(self.logger, log_level, propagate=False)
        setup_file_handler_for_child_process(self.logger, log_level, log_dir)
        setup_proxy_log_filters(suppress_broken_pipe=True, suppress_proxy_noise=True)

        secrets_file = os.getenv("FLOW_PROXY_SECRETS_FILE", "secrets.json")
        self.secrets_manager = SecretsManager()
        self.configs = self.secrets_manager.load_secrets(secrets_file)
        self.load_balancer = LoadBalancer(self.configs, self.logger)
        self.jwt_generator = JWTGenerator(self.logger)
        self.request_forwarder = RequestForwarder(self.logger)
        self.request_filter = RequestFilter(self.logger)
        self._client_lock = threading.Lock()
        self.http_client: httpx.Client | None = httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
            follow_redirects=False,
        )
        self.logger.info(
            "ProcessServices initialized with %d configs", len(self.configs)
        )

    def get_http_client(self) -> httpx.Client:
        """Return the process-level httpx.Client, rebuilding if previously marked dirty.

        Uses a dedicated _client_lock (NOT the class-level _lock) to avoid deadlock
        if this method is ever called from within a get() critical section.
        """
        if self.http_client is None:
            with self._client_lock:
                if self.http_client is None:
                    self.http_client = httpx.Client(
                        timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
                        follow_redirects=False,
                    )
                    self.logger.info("httpx.Client rebuilt after transport error")
        return self.http_client

    def mark_http_client_dirty(self) -> None:
        """Signal that the httpx.Client is in a broken state; next call to get_http_client()
        will close it and create a fresh one. Call this from TransportError handlers."""
        try:
            if self.http_client is not None:
                self.http_client.close()
        except Exception:
            pass
        self.http_client = None

    @classmethod
    def reset(cls) -> None:
        """Reset singleton. For testing only."""
        with cls._lock:
            if cls._instance is not None:
                try:
                    if cls._instance.http_client is not None:
                        cls._instance.http_client.close()
                except Exception:
                    pass
            cls._instance = None
