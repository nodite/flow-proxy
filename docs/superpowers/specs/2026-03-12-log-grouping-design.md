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

## Context Lifetime: Handler-Scoped

Context is set at the top of `handle_request()` / `before_upstream_connection()` and cleared in a `finally` block at the bottom of the same method. It is **not** held across connection lifecycle callbacks.

**Rationale:** proxy.py is multi-threaded and reuses OS threads. If context were held from `handle_request` through to `on_client_connection_close`, the interval between them on the same thread could accidentally be shared with a different connection. Scoping to the handler method eliminates this risk entirely.

**Trade-off:** Log lines emitted in `handle_upstream_chunk()`, `on_access_log()`, and `on_*_connection_close()` will not carry a `req_id` prefix. These are low-value operational callbacks; the full request trace (from `→ METHOD PATH` through `← STATUS`) happens entirely within `handle_request()` / `before_upstream_connection()`, so this is acceptable.

## Design

### New File: `flow_proxy_plugin/utils/log_context.py`

Provides thread-local context management (~50 lines):

```python
_local = threading.local()

def set_request_context(req_id: str, component: str) -> None:
    """Set req_id and component at request entry point."""
    _local.req_id = req_id
    _local.component = component

def clear_request_context() -> None:
    """Clear context. Called in finally block of handle_request / before_upstream_connection."""
    _local.req_id = ""
    _local.component = ""

def get_request_prefix() -> str:
    """Return '[req_id][COMPONENT] ' or '' if no context set."""
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

**Filter attachment:** The filter is added to the **logger object** (via `logger.addFilter(filter_instance)`) inside `setup_colored_logger()`. This is the correct attachment point for a message-mutating filter. A `LogRecord` is a shared mutable object passed to all handlers — if the filter were attached to each handler individually, `filter()` would be called once per handler, mutating `record.msg` twice and producing a double-prefix in all output. Attaching to the logger ensures `filter()` is called exactly once per `logger.log()` call, mutating `record.msg` once, before it is dispatched to all handlers.

**`record.msg` mutation safety:** The filter mutates `record.msg` before `record.args` is interpolated. For `%s`-style calls like `logger.info("Using config '%s'", name)`, `record.msg` is `"Using config '%s'"` and `record.args` is `(name,)` — prepending the prefix to `record.msg` is safe because `getMessage()` interpolates `record.args` into the final `record.msg` string. For f-string calls (used in `JWTGenerator`), `record.args` is `None` and `record.msg` is already fully interpolated — the prefix prepend is also safe.

### Modified: Plugin entry/exit points

**`web_server_plugin.py` — `handle_request()`:**
```python
def handle_request(self, request: HttpParser) -> None:
    req_id = secrets.token_hex(3)
    set_request_context(req_id, "WS")
    try:
        # ... existing body ...
    finally:
        clear_request_context()
```

**`proxy_plugin.py` — `before_upstream_connection()`:**
```python
def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
    req_id = secrets.token_hex(3)
    set_request_context(req_id, "PROXY")
    try:
        # ... existing body ...
    finally:
        clear_request_context()
```

No changes to `on_client_connection_close()` or `on_upstream_connection_close()`.

### Modified: Sub-component key methods

Each component wraps its log-emitting operations with `component_context`. The `component_context` context manager saves and restores the previous component tag, so nesting is safe.

**`base_plugin.py` — `_get_config_and_token()`:**

This method is the natural choke point for both `LoadBalancer` and `JWTGenerator` calls. Wrap the calls individually:

```python
def _get_config_and_token(self):
    with component_context("LB"):
        config = self.load_balancer.get_next_config()
    config_name = config.get("name", config.get("clientId", "unknown"))
    with component_context("JWT"):
        jwt_token = self.jwt_generator.generate_token(config)
    return config, config_name, jwt_token
```

The failover path uses the same pattern:

```python
def _get_config_and_token(self):
    with component_context("LB"):
        config = self.load_balancer.get_next_config()
    config_name = config.get("name", config.get("clientId", "unknown"))
    try:
        with component_context("JWT"):
            jwt_token = self.jwt_generator.generate_token(config)
        return config, config_name, jwt_token
    except ValueError as e:
        self.logger.error("Token generation failed for '%s': %s", config_name, str(e))
        with component_context("LB"):
            self.load_balancer.mark_config_failed(config)
            config = self.load_balancer.get_next_config()
        config_name = config.get("name", config.get("clientId", "unknown"))
        with component_context("JWT"):
            jwt_token = self.jwt_generator.generate_token(config)
        self.logger.info("Failover successful - using '%s'", config_name)
        return config, config_name, jwt_token
```

Note: `mark_config_failed()` and the second `get_next_config()` are both inside `component_context("LB")` so the failure log line at `LoadBalancer.mark_config_failed()` also carries the `LB` tag.

**`web_server_plugin.py` — `handle_request()`:**

Wrap the filter/forwarder operations:
```python
with component_context("FILTER"):
    filter_rule = self.request_filter.find_matching_rule(request, path)
    ...
with component_context("FWD"):
    self.logger.info("Sending request to backend: %s", target_url)
```

The backend response log lines remain under `"WS"` (restored after `component_context` exits).

**Note on `LoadBalancer._log_config_usage()`:** The `LB` tag is set via `component_context("LB")` in `_get_config_and_token()` before calling `get_next_config()`. Since `get_next_config()` calls `_log_config_usage()` internally, the `LB` tag is already active when those log lines emit. No changes needed inside `LoadBalancer` itself. This works because `LoadBalancer._logger` is the same `logging.Logger` instance as `ProcessServices.logger` (set at `process_services.py:49`) — the same logger the filter is attached to.

## File Change Summary

| File | Change |
|---|---|
| `utils/log_context.py` | **New** — thread-local context + `component_context` context manager |
| `utils/logging.py` | Add `RequestContextFilter`; attach to the `flow_proxy` logger object in `setup_colored_logger()` |
| `plugins/web_server_plugin.py` | `try/finally` in `handle_request()` for set/clear; `component_context` for FILTER and FWD |
| `plugins/proxy_plugin.py` | `try/finally` in `before_upstream_connection()` for set/clear |
| `plugins/base_plugin.py` | `component_context("LB")` and `component_context("JWT")` in `_get_config_and_token()` |

## Testing

- **`test_log_context.py`** (new):
  - `set_request_context` / `clear_request_context` / `get_request_prefix` basic behavior
  - `get_request_prefix` returns `""` when no context is set (startup logs, callbacks without request context)
  - `component_context` restores previous component on normal exit
  - `component_context` restores previous component when exception is raised inside
  - `component_context` called with no enclosing request context (req_id not set) — should emit no prefix
  - Thread isolation: two threads setting different contexts do not bleed into each other
- **`test_logging.py`** (extend):
  - `RequestContextFilter` prepends prefix when context is set
  - `RequestContextFilter` is a no-op when no context is set
  - Filter is attached at logger level, not handler level (assert `filter in logger.filters`)
- **Existing tests:** All plugin tests must continue to pass. Since context is cleared in `finally` and tests do not set context, no interference is expected.
