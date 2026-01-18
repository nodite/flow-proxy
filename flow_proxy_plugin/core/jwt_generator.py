"""JWT token generator for Flow LLM Proxy authentication."""

import logging
import threading
import time
from typing import Any

import jwt


class JWTGenerator:
    """Generates and caches JWT tokens for Flow LLM Proxy authentication."""

    # Token cache: {client_id: (token, expiry_time)}
    _cache: dict[str, tuple[str, float]] = {}
    _lock = threading.Lock()
    _ttl = 3600  # 1 hour
    _margin = 300  # Refresh 5 minutes before expiry

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Initialize JWT generator.

        Args:
            logger: Optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)

    def generate_token(self, config: dict[str, str]) -> str:
        """Generate or return cached JWT token.

        Args:
            config: Auth config with clientId, clientSecret, and tenant

        Returns:
            JWT token string

        Raises:
            ValueError: If config is invalid
        """
        # Validate config
        required = ["clientId", "clientSecret", "tenant"]
        missing = [f for f in required if not config.get(f, "").strip()]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

        client_id = config["clientId"]
        now = time.time()

        # Check cache
        with self._lock:
            if client_id in self._cache:
                token, expiry = self._cache[client_id]
                if now < expiry - self._margin:
                    self.logger.debug(f"Using cached token for {client_id}")
                    return token

        # Generate new token
        payload = {
            "clientId": config["clientId"],
            "clientSecret": config["clientSecret"],
            "tenant": config["tenant"],
        }
        token = jwt.encode(payload, config["clientSecret"], algorithm="HS256")

        # Cache it
        with self._lock:
            self._cache[client_id] = (token, now + self._ttl)

        self.logger.info(f"Generated JWT token for {client_id}")
        return token

    def create_jwt_payload(self, config: dict[str, str]) -> dict[str, Any]:
        """Create JWT payload from config.

        Args:
            config: Authentication configuration

        Returns:
            JWT payload dictionary
        """
        return {
            "clientId": config["clientId"],
            "clientSecret": config["clientSecret"],
            "tenant": config["tenant"],
        }

    def validate_token(self, token: str, secret: str) -> bool:
        """Validate JWT token.

        Args:
            token: JWT token to validate
            secret: Secret key

        Returns:
            True if valid
        """
        try:
            decoded = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                options={"verify_exp": False},
            )
            required = ["clientId", "clientSecret", "tenant"]
            return all(f in decoded for f in required)
        except jwt.InvalidTokenError:
            return False

    def clear_cache(self) -> None:
        """Clear token cache."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self.logger.info(f"Cleared {count} cached tokens")

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Cache stats dictionary
        """
        with self._lock:
            now = time.time()
            valid = sum(
                1 for _, exp in self._cache.values() if now < exp - self._margin
            )
            return {
                "total": len(self._cache),
                "valid": valid,
                "expired": len(self._cache) - valid,
            }
