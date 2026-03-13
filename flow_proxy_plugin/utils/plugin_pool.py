"""Generic thread-safe pool for proxy.py plugin instances."""

import threading
from collections import deque
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class PluginPool(Generic[T]):
    """Pool of pre-initialized plugin instances.

    On acquire: returns a pooled instance with rebound connection state,
    or creates a new instance if pool is empty.

    On release: returns instance to pool (discards if pool is at max_size).

    IMPORTANT: acquire() uses object.__new__() to create new instances to
    avoid infinite recursion through the __new__ override on plugin classes.
    """

    def __init__(self, plugin_cls: type[T], max_size: int = 64) -> None:
        self._pool: deque[T] = deque()
        self._lock = threading.Lock()
        self._plugin_cls = plugin_cls
        self.max_size = max_size

    def acquire(self, *args: Any, **kwargs: Any) -> T:
        """Return a pooled instance (rebound) or create a new one."""
        instance: T | None = None
        with self._lock:
            if self._pool:
                instance = self._pool.pop()

        if instance is not None:
            instance._rebind(*args, **kwargs)  # type: ignore[attr-defined]
            return instance

        # Pool empty — create new instance bypassing __new__ override to
        # avoid infinite recursion (cls(*args) would re-enter __new__).
        new_instance: T = object.__new__(self._plugin_cls)
        new_instance.__init__(*args, **kwargs)  # type: ignore[misc]  # pylint: disable=unnecessary-dunder-call
        return new_instance

    def release(self, instance: T) -> None:
        """Return instance to pool after clearing per-request state."""
        instance._reset_request_state()  # type: ignore[attr-defined]
        with self._lock:
            if len(self._pool) < self.max_size:
                self._pool.append(instance)
        # If pool is full, instance is silently dropped for GC
