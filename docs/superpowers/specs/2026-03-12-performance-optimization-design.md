# Performance, Logging, and Concurrency Optimization Design

**Date:** 2026-03-12
**Status:** Approved
**Scope:** Breaking refactoring — replace SharedComponentManager with ProcessServices singleton, introduce PluginPool for plugin instance reuse, eliminate redundant initialization logging.

---

## Problem Statement

The current architecture creates a new plugin instance for every incoming connection. Each instantiation:

1. Calls `_setup_logging()` — clears and re-adds handlers unconditionally
2. Calls `_initialize_components()` — logs `Initializing FlowProxyWebServerPlugin...` every time
3. Creates a new `httpx.Client` per request — no connection pool reuse, TCP handshake overhead on every request

Although `SharedComponentManager` prevents duplicate `LoadBalancer` / `configs` initialization, it doesn't prevent the plugin `__init__` overhead that fires for every connection.

---

## Goals

- Plugin `__init__` becomes zero-cost after first invocation (no logging, no component setup)
- `Initializing FlowProxyWebServerPlugin...` logged exactly once per process
- `httpx.Client` connection pool shared across all requests in a process
- Logging setup runs exactly once per process, via ProcessServices
- All shared state owned by one place: `ProcessServices`

---

## Architecture

### Component Map (After)

```
ProcessServices (singleton, per-process)
├── logger                  # initialized once
├── secrets_manager
├── configs
├── load_balancer
├── jwt_generator
├── request_forwarder
├── request_filter
└── http_client             # httpx.Client, persistent connection pool

PluginPool[T] (module-level, per-plugin-class)
├── _pool: deque[T]
├── max_size: int
├── acquire(uid, flags, ...) → T
└── release(instance: T) → None

FlowProxyWebServerPlugin / FlowProxyPlugin (thin request-scoped handlers)
├── __new__  → delegates to PluginPool.acquire()
├── __init__ → if _pooled: _rebind() only; else: full init (once)
└── on_client_connection_close() → PluginPool.release(self)
```

### Deleted

- `flow_proxy_plugin/utils/plugin_base.py` (`SharedComponentManager`, `initialize_plugin_components()`)

### New Files

- `flow_proxy_plugin/utils/process_services.py` — `ProcessServices` singleton
- `flow_proxy_plugin/utils/plugin_pool.py` — `PluginPool[T]` generic pool

---

## ProcessServices

`ProcessServices` is a thread-safe singleton initialized lazily on first access (after fork, in the child process).

```python
class ProcessServices:
    _instance: Optional["ProcessServices"] = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "ProcessServices":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = cls.__new__(cls)
                    instance._initialize()
                    cls._instance = instance
        return cls._instance

    def _initialize(self) -> None:
        # logging (once per process)
        self.logger = setup_colored_logger()
        setup_file_handler_for_child_process(self.logger)
        # components
        self.secrets_manager = SecretsManager()
        self.configs = self.secrets_manager.configs
        self.load_balancer = LoadBalancer(self.configs)
        self.jwt_generator = JWTGenerator()
        self.request_forwarder = RequestForwarder()
        self.request_filter = RequestFilter(self.logger)
        # persistent httpx connection pool
        self.http_client = httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=600.0),
            follow_redirects=False,
        )

    @classmethod
    def reset(cls) -> None:
        """For testing only. Resets singleton state."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.http_client.close()
            cls._instance = None
```

**httpx.Client resilience**: If a `TransportError` occurs and the client is in a broken state, the error handler sets `_instance.http_client = None`. The next request that calls `ProcessServices.get()._get_http_client()` detects `None` and rebuilds.

---

## PluginPool

```python
class PluginPool(Generic[T]):
    def __init__(self, plugin_cls: type[T], max_size: int = 64) -> None:
        self._pool: deque[T] = deque()
        self._lock = threading.Lock()
        self._plugin_cls = plugin_cls
        self.max_size = max_size

    def acquire(self, *args: Any, **kwargs: Any) -> T:
        with self._lock:
            if self._pool:
                instance = self._pool.pop()
                instance._rebind(*args, **kwargs)   # rebind connection state
                return instance
        # pool empty — create new instance (full __init__ runs)
        return self._plugin_cls(*args, **kwargs)

    def release(self, instance: T) -> None:
        instance._reset_request_state()
        with self._lock:
            if len(self._pool) < self.max_size:
                self._pool.append(instance)
        # if pool full, instance is simply dropped (GC handles it)
```

**max_size**: Defaults to `64`, configurable via `FLOW_PROXY_PLUGIN_POOL_SIZE` env var. Matches the default proxy.py thread pool concurrency ceiling.

---

## Plugin Refactoring

### BaseFlowProxyPlugin

Becomes a thin mixin. `__init__` no longer calls `_setup_logging()` or `_initialize_components()`. Instead:

```python
class BaseFlowProxyPlugin:
    _log_once_done: bool = False   # class variable

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._services = ProcessServices.get()
        self.logger = self._services.logger
        self.load_balancer = self._services.load_balancer
        self.jwt_generator = self._services.jwt_generator
        self.request_forwarder = self._services.request_forwarder
        self.request_filter = self._services.request_filter
        if not BaseFlowProxyPlugin._log_once_done:
            BaseFlowProxyPlugin._log_once_done = True
            self.logger.info("Initializing %s", self.__class__.__name__)
        self._pooled = False

    def _rebind(self, uid: str, flags: Any, client: Any,
                event_queue: Any, upstream_conn_pool: Any = None) -> None:
        """Rebind connection-specific state when reusing a pooled instance."""
        # call parent rebind to reset proxy.py connection state
        super().__init__(uid, flags, client, event_queue, upstream_conn_pool)
        self._pooled = True

    def _reset_request_state(self) -> None:
        """Clear any per-request state before returning to pool."""
        pass  # subclasses override if they hold per-request state
```

### FlowProxyWebServerPlugin

```python
_web_pool: PluginPool["FlowProxyWebServerPlugin"] = PluginPool(
    FlowProxyWebServerPlugin,
    max_size=int(os.environ.get("FLOW_PROXY_PLUGIN_POOL_SIZE", "64")),
)

class FlowProxyWebServerPlugin(HttpWebServerBasePlugin, BaseFlowProxyPlugin):

    def __new__(cls, uid, flags, client, event_queue, upstream_conn_pool=None):
        return _web_pool.acquire(uid, flags, client, event_queue, upstream_conn_pool)

    def __init__(self, uid, flags, client, event_queue, upstream_conn_pool=None):
        if self._pooled:
            return   # _rebind() already called by pool.acquire()
        super().__init__(uid, flags, client, event_queue, upstream_conn_pool)

    def handle_request(self, request: HttpParser) -> None:
        # uses self._services.http_client instead of creating new httpx.Client
        ...

    def on_client_connection_close(self) -> None:
        _web_pool.release(self)
```

Same pattern applies to `FlowProxyPlugin`.

---

## Error Handling

| Scenario | Handling |
|----------|----------|
| Exception in `handle_request()` | proxy.py calls `on_client_connection_close()` regardless; `release()` calls `_reset_request_state()` first |
| `httpx.TransportError` in streaming | Mark `_services.http_client` as dirty; rebuild on next access |
| Pool full | Instance dropped silently; GC handles cleanup |
| `ProcessServices._initialize()` fails | Exception propagates to proxy.py connection handler; logged as fatal |

---

## Testing

### New test files

| File | Coverage |
|------|----------|
| `tests/test_plugin_pool.py` | acquire/release, pool reuse, max_size eviction, concurrent acquire |
| `tests/test_process_services.py` | singleton init, reset(), httpx client reuse, fork-safe lazy init |

### Modified test files

| File | Change |
|------|--------|
| `tests/test_shared_state.py` | Replace `SharedComponentManager` refs with `ProcessServices` |
| `tests/test_plugin.py` | Update mock patterns for new `__new__`/`__init__` contract |
| `tests/test_web_server_plugin.py` | Mock `ProcessServices.get()` instead of per-instance setup |
| `tests/test_thread_safety.py` | Add pool concurrent acquire/release tests |

---

## Breaking Changes

| What changes | Impact |
|-------------|--------|
| `utils/plugin_base.py` deleted | Any import of `SharedComponentManager` or `initialize_plugin_components` must update |
| Plugin `__init__` no longer idempotent | Tests that call `__init__` directly must use `ProcessServices.reset()` between runs |
| `httpx.Client` is process-level | Tests mocking `httpx.Client` need module-level patching, not instance-level |
| `_setup_logging()` removed from plugins | Tests asserting logger handler count on plugin instances will fail |

---

## Environment Variables (New)

| Variable | Default | Description |
|----------|---------|-------------|
| `FLOW_PROXY_PLUGIN_POOL_SIZE` | `64` | Max pooled instances per plugin class |
