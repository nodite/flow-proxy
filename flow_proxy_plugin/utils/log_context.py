"""Thread-local request context for log grouping.

Known design limitation: components that create their own logger directly (e.g.
LoadBalancer in unit tests without ProcessServices) will not carry a prefix when
no request context is active — get_request_prefix() returns '' and the filter is a
no-op. This is expected behaviour: startup logs and test-only logs emit without a
prefix, while all in-request log lines (emitted inside handle_request /
before_upstream_connection) carry [req_id][COMPONENT] prefixes.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


def set_request_context(req_id: str, component: str) -> None:
    """Set req_id and component at request entry point."""
    _local.req_id = req_id
    _local.component = component


def clear_request_context() -> None:
    """Clear context. Call in finally block of handle_request / before_upstream_connection."""
    _local.req_id = ""
    _local.component = ""


def get_request_prefix() -> str:
    """Return '[req_id][COMPONENT] ' or '' if no context is set."""
    req_id = getattr(_local, "req_id", "")
    component = getattr(_local, "component", "")
    if not req_id:
        return ""
    return f"[{req_id}][{component}] " if component else f"[{req_id}] "


@contextmanager
def component_context(name: str) -> Iterator[None]:
    """Temporarily override component tag, restoring previous value on exit (even on exception)."""
    old = getattr(_local, "component", "")
    _local.component = name
    try:
        yield
    finally:
        _local.component = old
