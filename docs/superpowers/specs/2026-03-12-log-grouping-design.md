# Log Grouping with Request ID and Component Prefix

**Date:** 2026-03-12
**Status:** Approved

## Problem

All request-related log messages share a single `flow_proxy` logger. Under concurrent load, logs from multiple requests interleave, making it impossible to trace a single request end-to-end or understand which components were involved.

Example of the problem:
```
[INFO] → POST /v1/messages
[INFO] → POST /v1/messages          ← second request interleaves
[INFO] Using config 'flow-proxy-cit'
[INFO] Using config 'flow-proxy-apac'
[INFO] Generated JWT token for 9e69241a-...
[INFO] Generated JWT token for 3dfc4cf2-...
```

## Goal

Add `[req_id][COMPONENT]` prefixes to all log lines so each request can be traced in isolation and component call order is visible:

```
[a1b2c3][WS]     → POST /v1/messages
[a1b2c3][LB]     Using config 'flow-proxy-cit' (request #1)
[a1b2c3][JWT]    Generated JWT token for 9e69241a-...
[a1b2c3][FWD]    Sending request to backend: https://...
[a1b2c3][WS]     Backend response: 200 OK
[a1b2c3][WS]     ← 200 OK [flow-proxy-cit]
```

## Approach: logging.Filter + threading.local

Use a `threading.local` store to hold `(req_id, component)` for the current thread. A `logging.Filter` reads this context and prepends `[req_id][COMPONENT]` to every log message passing through the `flow_proxy` logger. Components temporarily override the `component` field around their key operations.

This approach was chosen over:
- **Direct `extra=` on every call**: too invasive, easy to miss calls
- **contextvar + LogAdapter**: no benefit over `threading.local` in a sync multi-threaded model; requires replacing `self.logger` everywhere

## Component Tags

| Component | Tag |
|---|---|
| FlowProxyWebServerPlugin | `WS` |
| FlowProxyPlugin | `PROXY` |
| JWTGenerator | `JWT` |
| LoadBalancer | `LB` |
| RequestForwarder | `FWD` |
| RequestFilter | `FILTER` |

## Design

### New File: `flow_proxy_plugin/utils/log_context.py`

Provides thread-local context management (~40 lines):

```python
_local = threading.local()

def set_request_context(req_id: str, component: str) -> None:
    """Set req_id and component at request entry point."""

def clear_request_context() -> None:
    """Clear context at request exit point."""

def get_request_prefix() -> str:
    """Return '[req_id][COMPONENT] ' or '' if no context set."""

@contextmanager
def component_context(name: str) -> Iterator[None]:
    """Temporarily override component tag, restoring previous value on exit."""
```

Request IDs are 6-character lowercase hex strings generated with `secrets.token_hex(3)`.

### Modified: `flow_proxy_plugin/utils/logging.py`

Add `RequestContextFilter` class:

```python
class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        prefix = get_request_prefix()
        if prefix:
            record.msg = f"{prefix}{record.msg}"
        return True
```

Inject this filter in `setup_colored_logger()` and `setup_file_handler_for_child_process()` so it applies to all handlers on the `flow_proxy` logger.

### Modified: Plugin entry/exit points

**`web_server_plugin.py`:**
- `handle_request()` start: `set_request_context(secrets.token_hex(3), "WS")`
- `on_client_connection_close()`: `clear_request_context()`

**`proxy_plugin.py`:**
- `before_upstream_connection()` start: `set_request_context(secrets.token_hex(3), "PROXY")`
- `on_upstream_connection_close()`: `clear_request_context()`

### Modified: Sub-component key methods

Each component wraps its key logging operations with `component_context`:

**`jwt_generator.py`** — `get_token()` / `_generate_token()`:
```python
with component_context("JWT"):
    # existing logic
```

**`load_balancer.py`** — `get_next_config_context()`:
```python
with component_context("LB"):
    # existing logic
```

**`request_forwarder.py`** — `modify_request_headers()`:
```python
with component_context("FWD"):
    # existing logic
```

**`request_filter.py`** — `find_matching_rule()` / `filter_*()`:
```python
with component_context("FILTER"):
    # existing logic
```

## File Change Summary

| File | Change |
|---|---|
| `utils/log_context.py` | **New** — thread-local context + context manager |
| `utils/logging.py` | Add `RequestContextFilter`; inject in handler setup functions |
| `plugins/web_server_plugin.py` | Set context at entry; clear at connection close |
| `plugins/proxy_plugin.py` | Set context at entry; clear at connection close |
| `core/jwt_generator.py` | Wrap key method with `component_context("JWT")` |
| `core/load_balancer.py` | Wrap key method with `component_context("LB")` |
| `core/request_forwarder.py` | Wrap key method with `component_context("FWD")` |
| `plugins/request_filter.py` | Wrap key methods with `component_context("FILTER")` |

## Testing

- Unit tests for `log_context.py`: set/clear/get, `component_context` nesting and restore on exception
- Unit test for `RequestContextFilter`: with and without context set
- Existing plugin tests must still pass (context is cleared between requests via `clear_request_context`)
- Thread-safety: verify concurrent requests do not bleed context across threads
