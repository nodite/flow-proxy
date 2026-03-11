# Performance, Logging & Concurrency Optimization Design

**Date**: 2026-03-11
**Status**: Approved

## Problem Statement

In multi-threaded mode, proxy.py instantiates a new plugin object for every incoming connection. This causes:

1. `"Initializing FlowProxyWebServerPlugin..."` logged on every request (INFO level noise)
2. `_setup_logging()` called per-instance — clears and rebuilds handlers repeatedly
3. `httpx.Client()` created per-request — TCP connection pool never reused
4. `JWTGenerator` / `RequestForwarder` / `RequestFilter` instantiated per-connection unnecessarily

## Goals

- Eliminate repeated initialization log noise
- Initialize logging exactly once per process
- Reuse `httpx.Client` connection pool across requests (process-level)
- Reuse plugin instances via a pool (avoid repeated `__init__` overhead)

## Non-Goals

- Changing proxy.py internals
- Changing the external CLI interface
- Changing JWT caching logic (already class-level and effective)

---

## Architecture

### Core Insight

Python's `__new__` + `__init__` mechanism allows interception of proxy.py's plugin instantiation without modifying the framework. Combined with proxy.py's `on_client_connection_close()` lifecycle hook, we can implement a true instance pool.

```
proxy.py calls: Plugin(uid, flags, client, event_queue, upstream_conn_pool)
       ↓
__new__  → returns pooled instance (or creates new placeholder)
       ↓
__init__ → if pooled: _rebind(uid, flags, client, ...)  [< 1μs]
           if new:    full init once, mark _pooled=True
       ↓
handle_request() / before_upstream_connection()
       ↓
on_client_connection_close() → release instance back to pool
```

---

## Component Design

### 1. `utils/process_services.py` (new — replaces `utils/plugin_base.py`)

Process-level singleton owning all shared resources. Lazy-initialized on first access after `fork()`, ensuring fork safety.

```python
class ProcessServices:
    _instance: Optional["ProcessServices"] = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "ProcessServices":
        """Thread-safe lazy singleton accessor."""
        ...

    # Resources initialized once per process
    logger: logging.Logger
    secrets_manager: SecretsManager
    configs: list
    load_balancer: LoadBalancer
    jwt_generator: JWTGenerator
    request_forwarder: RequestForwarder
    request_filter: RequestFilter
    http_client: httpx.Client        # persistent connection pool

    def reset(self) -> None:
        """Reset singleton — for testing only."""
        ...
```

**httpx.Client timeout config** (centralized here): `connect=30s, read=600s, write=30s, pool=30s`

**httpx.Client recovery**: if a `TransportError` signals client corruption, mark for rebuild; next `ProcessServices.get()` recreates it.

**Logging**: `_initialize()` calls `setup_colored_logger()`, `setup_file_handler_for_child_process()`, **and** `setup_proxy_log_filters(suppress_broken_pipe=True, suppress_proxy_noise=True)` once. All plugin instances share `ProcessServices.logger`. The `setup_proxy_log_filters` call must not be lost in migration.

### 2. `utils/plugin_pool.py` (new)

Generic thread-safe pool for plugin instances.

```python
class PluginPool(Generic[T]):
    def __init__(self, plugin_cls: type[T], max_size: int = 64) -> None: ...

    def acquire(self, uid, flags, client, event_queue, upstream_conn_pool) -> T:
        """Return pooled instance (rebound) or create new.

        When pool is empty, creates a new instance by calling
        object.__new__(self._plugin_cls) directly — bypassing __new__ override
        to avoid infinite recursion — then calls __init__ normally to trigger
        _full_init on the fresh instance.
        """
        ...

    def release(self, instance: T) -> None:
        """Return instance to pool; discard if pool at max_size."""
        ...
```

**Pool size**: default `64`, configurable via `FLOW_PROXY_PLUGIN_POOL_SIZE` env var.

**On release**: call `instance._reset_request_state()` to clear any per-request dirty state before returning to pool.

**Avoiding infinite recursion**: `acquire()` must NOT call `cls(...)` to create new instances (that would re-enter `__new__`). Instead it uses `object.__new__(cls)` then delegates to `__init__` with `_pooled=False`.

### 3. `plugins/base_plugin.py` (modified)

`BaseFlowProxyPlugin` becomes a thin mixin. `__init__` is removed — all initialization delegated to `ProcessServices`.

```python
class BaseFlowProxyPlugin:
    _pooled: bool = False  # class-level default; instance attr shadows it after full init

    def _full_init(self) -> None:
        """Called once on first instantiation. Sets up services reference."""
        self._services = ProcessServices.get()
        self.logger = self._services.logger
        self.load_balancer = self._services.load_balancer
        self.jwt_generator = self._services.jwt_generator
        self.request_forwarder = self._services.request_forwarder

    def _rebind(self, uid, flags, client, event_queue, upstream_conn_pool) -> None:
        """Rebind connection-specific state for pooled reuse."""
        self.uid = uid
        self.flags = flags
        self.client = client
        self.event_queue = event_queue
        self.upstream_conn_pool = upstream_conn_pool

    def _reset_request_state(self) -> None:
        """Clear per-request state before returning to pool.

        Currently no per-request instance state exists in either subclass beyond
        what proxy.py sets (uid, client, etc.) — those are rebound by _rebind().
        This hook is provided for safety; subclasses override if they assign any
        self.* attribute during request handling (e.g. partial response buffers).
        """
        pass
```

### 4. `plugins/web_server_plugin.py` (modified)

Pool is a module-level `None` sentinel, lazily initialized inside `__new__` to avoid circular reference (class not yet defined at module load time):

```python
_web_pool: Optional[PluginPool["FlowProxyWebServerPlugin"]] = None
_web_pool_lock = threading.Lock()

class FlowProxyWebServerPlugin(HttpWebServerBasePlugin, BaseFlowProxyPlugin):

    def __new__(cls, *args, **kwargs):
        global _web_pool
        if _web_pool is None:
            with _web_pool_lock:
                if _web_pool is None:
                    _web_pool = PluginPool(
                        cls,
                        max_size=int(os.getenv("FLOW_PROXY_PLUGIN_POOL_SIZE", "64")),
                    )
        return _web_pool.acquire(*args, **kwargs)

    def __init__(self, uid, flags, client, event_queue, upstream_conn_pool=None):
        if self._pooled:
            # Pooled reuse: rebind connection-specific state only
            self._rebind(uid, flags, client, event_queue, upstream_conn_pool)
            return
        # First-time init: call parent to bind connection state
        super().__init__(uid, flags, client, event_queue, upstream_conn_pool)
        try:
            self._full_init()
            self.request_filter = RequestFilter(self.logger)
            self._pooled = True  # only set after successful full init
            self.logger.info("FlowProxyWebServerPlugin initialized (pooled)")
        except Exception:
            # Full init failed — do NOT set _pooled=True; instance will not be
            # returned to pool and will be GC-collected
            raise

    def on_client_connection_close(self) -> None:
        """Release instance back to pool on connection close."""
        if _web_pool is not None and self._pooled:
            _web_pool.release(self)
```

`handle_request()` uses `self._services.http_client` instead of creating a new `httpx.Client()` per call.

**httpx.Client thread safety**: `httpx.Client` is documented as thread-safe for concurrent requests. On `TransportError`, the handler catches the error, sets `self._services.mark_http_client_dirty()`, and the next call to `ProcessServices.get_http_client()` atomically replaces the client. Threads that already hold a streaming response to completion are unaffected; the old client is GC-collected after all references drop.

### 5. `plugins/proxy_plugin.py` (modified)

Same pool pattern as `web_server_plugin.py` with module-level `_proxy_pool` sentinel.

**Release hook differs**: `HttpProxyBasePlugin` does not have `on_client_connection_close()`. The correct release hook for `FlowProxyPlugin` is `on_upstream_connection_close()`, which already exists in the current codebase. Pool release goes there:

```python
def on_upstream_connection_close(self) -> None:
    if _proxy_pool is not None and self._pooled:
        _proxy_pool.release(self)
```

`HttpProxyBasePlugin.__init__` has the same signature as `HttpWebServerBasePlugin.__init__`:
`(uid, flags, client, event_queue, upstream_conn_pool=None)`

---

## Files Changed

| File | Action |
|------|--------|
| `utils/process_services.py` | **New** — replaces `plugin_base.py` |
| `utils/plugin_pool.py` | **New** |
| `utils/plugin_base.py` | **Deleted** |
| `plugins/base_plugin.py` | **Modified** — remove `__init__`, add `_full_init` / `_rebind` / `_reset_request_state` |
| `plugins/web_server_plugin.py` | **Modified** — pool integration, use shared httpx client |
| `plugins/proxy_plugin.py` | **Modified** — pool integration |
| `tests/test_process_services.py` | **New** |
| `tests/test_plugin_pool.py` | **New** |
| `tests/test_shared_state.py` | **Modified** — update for ProcessServices |
| `tests/test_plugin.py` | **Modified** — update mock scope |
| `tests/test_web_server_plugin.py` | **Modified** — update mock scope |

---

## Breaking Changes

| Change | Impact |
|--------|--------|
| `utils/plugin_base.py` deleted | Any external code importing `SharedComponentManager` or `initialize_plugin_components` must update |
| Plugin `__init__` no longer runs full init every call | Tests mocking plugin init need scope adjustment |
| `httpx.Client` is now process-level | Tests mocking `httpx.Client` must mock at `ProcessServices` level |

---

## Error Handling

- **Dirty pool state**: `release()` calls `_reset_request_state()` before returning instance to pool
- **First-init failure**: if `_full_init()` raises, `_pooled` remains `False` — instance is never returned to pool, GC-collected normally
- **httpx.Client corruption**: `TransportError` sets `ProcessServices._http_client_dirty = True`; `get_http_client()` atomically closes old client and creates fresh one under lock. In-flight streaming responses on old client complete normally before GC.
- **Pool slot leak** (hook not called): if `on_client_connection_close()` / `on_upstream_connection_close()` is not called by proxy.py on crash/timeout, slots are not returned. Pool fills to `max_size` and overflows gracefully — new instances are created as throw-aways. This is acceptable degradation; no deadlock or starvation.
- **Pool overflow**: instances beyond `max_size` are silently discarded (GC-collected); next request creates a new instance — normal degradation behavior

---

## Testing Strategy

| Test File | Coverage |
|-----------|---------|
| `test_plugin_pool.py` | acquire/release, concurrent access, pool-full discard |
| `test_process_services.py` | singleton init, reset, httpx client reuse |
| `test_thread_safety.py` | extend: pool correctness under concurrency |
