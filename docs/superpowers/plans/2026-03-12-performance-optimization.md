# Performance Optimization Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `SharedComponentManager` with a process-level `ProcessServices` singleton, introduce `PluginPool` for true plugin instance reuse, and eliminate all per-connection initialization overhead.

**Architecture:** `ProcessServices` is a lazily-initialized per-process singleton (fork-safe) owning all shared resources including a persistent `httpx.Client`. `PluginPool` intercepts proxy.py's instantiation via `__new__` override and rebinds only connection-specific state on reuse — avoiding the full `__init__` path on every connection. Plugin `__init__` is guarded by `self._pooled` and becomes a no-op on reuse.

**Tech Stack:** Python 3.12+, proxy.py framework, httpx, threading, Generic types

---

## Pre-flight: Critical Design Notes

Before touching any code, every implementer must understand:

1. **`__new__` recursion hazard**: `PluginPool.acquire()` must NOT call `PluginClass(*args)` to create new instances — that re-enters `__new__` causing infinite recursion. Use `object.__new__(cls)` then call `.__init__()` manually.

2. **Double `__init__` call**: Python's `type.__call__` always calls `__init__` after `__new__` returns. Since `pool.acquire()` also calls `__init__` for new instances, `__init__` will fire twice on first creation. Guard with `if self._pooled: return` at the top of every plugin `__init__`.

3. **`_rebind` must call proxy.py parent directly**: `FlowProxyWebServerPlugin._rebind` calls `HttpWebServerBasePlugin.__init__(self, ...)` explicitly — NOT `super().__init__()`. This is an intentional departure from the spec's MRO-based `super()` design. The MRO for `FlowProxyWebServerPlugin` is `→ HttpWebServerBasePlugin → BaseFlowProxyPlugin → object`. If `_rebind` used `super().__init__()` from `BaseFlowProxyPlugin`, it would call `object.__init__()`, not the proxy.py parent. Since `BaseFlowProxyPlugin` has no `__init__`, the MRO-cooperative pattern would require calling `HttpWebServerBasePlugin.__init__` from the concrete class anyway. Explicit is correct here.

4. **`setup_proxy_log_filters`**: Currently called in `BaseFlowProxyPlugin._setup_logging()`. Must be preserved in `ProcessServices._initialize()` — do not drop it.

5. **Forward proxy release hook**: `FlowProxyPlugin` releases to pool in `on_upstream_connection_close()` (which already exists). Web server plugin uses `on_client_connection_close()` — confirmed present in `HttpWebServerBasePlugin` via `dir()` inspection.

6. **`plugins/base.py`** is a legacy unused file — delete it along with `utils/plugin_base.py`.

---

## Chunk 1: ProcessServices

### Task 1: Create `ProcessServices` singleton

**Files:**
- Create: `flow_proxy_plugin/utils/process_services.py`
- Test: `tests/test_process_services.py`

- [ ] **Step 1: Write failing test for singleton behavior**

```python
# tests/test_process_services.py
import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from flow_proxy_plugin.utils.process_services import ProcessServices


@pytest.fixture(autouse=True)
def reset_singleton(tmp_path):
    """Reset ProcessServices singleton between tests."""
    ProcessServices.reset()
    secrets = [{"name": "c1", "clientId": "id1", "clientSecret": "s1", "tenant": "t1"}]
    f = tmp_path / "secrets.json"
    f.write_text(json.dumps(secrets))
    os.environ["FLOW_PROXY_SECRETS_FILE"] = str(f)
    yield
    ProcessServices.reset()
    os.environ.pop("FLOW_PROXY_SECRETS_FILE", None)


def _make_services():
    with patch("flow_proxy_plugin.utils.process_services.setup_colored_logger"):
        with patch("flow_proxy_plugin.utils.process_services.setup_file_handler_for_child_process"):
            with patch("flow_proxy_plugin.utils.process_services.setup_proxy_log_filters"):
                return ProcessServices.get()


def test_singleton_returns_same_instance():
    s1 = _make_services()
    s2 = _make_services()
    assert s1 is s2


def test_reset_clears_singleton():
    s1 = _make_services()
    ProcessServices.reset()
    s2 = _make_services()
    assert s1 is not s2


def test_thread_safe_singleton():
    results = []
    def get():
        results.append(_make_services())
    threads = [threading.Thread(target=get) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert all(r is results[0] for r in results)


def test_has_required_attributes():
    svc = _make_services()
    for attr in ("logger", "secrets_manager", "configs", "load_balancer",
                 "jwt_generator", "request_forwarder", "request_filter", "http_client"):
        assert hasattr(svc, attr), f"Missing attribute: {attr}"


def test_http_client_is_reused():
    svc = _make_services()
    client1 = svc.http_client
    client2 = _make_services().http_client
    assert client1 is client2


def test_reset_closes_http_client():
    svc = _make_services()
    with patch.object(svc.http_client, "close") as mock_close:
        ProcessServices.reset()
    mock_close.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_process_services.py -v
```
Expected: `ModuleNotFoundError: No module named 'flow_proxy_plugin.utils.process_services'`

- [ ] **Step 3: Implement `ProcessServices`**

```python
# flow_proxy_plugin/utils/process_services.py
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


class ProcessServices:
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
        self._client_lock = threading.Lock()  # dedicated lock for http_client rebuild
        self.http_client = httpx.Client(
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
            follow_redirects=False,
        )
        self.logger.info(
            "ProcessServices initialized with %d configs", len(self.configs)
        )

    @classmethod
    def reset(cls) -> None:
        """Reset singleton. For testing only."""
        with cls._lock:
            if cls._instance is not None:
                try:
                    cls._instance.http_client.close()
                except Exception:
                    pass
            cls._instance = None
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_process_services.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add flow_proxy_plugin/utils/process_services.py tests/test_process_services.py
git commit -m "feat: add ProcessServices process-level singleton"
```

---

## Chunk 2: PluginPool

### Task 2: Create `PluginPool`

**Files:**
- Create: `flow_proxy_plugin/utils/plugin_pool.py`
- Test: `tests/test_plugin_pool.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_plugin_pool.py
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from flow_proxy_plugin.utils.plugin_pool import PluginPool


class FakePlugin:
    """Minimal plugin stand-in for pool testing."""
    _pooled: bool = False

    def __init__(self, uid: str, **kwargs):
        self.uid = uid
        self._pooled = True

    def _rebind(self, uid: str, **kwargs):
        self.uid = uid

    def _reset_request_state(self):
        pass


def test_acquire_creates_new_when_pool_empty():
    pool = PluginPool(FakePlugin, max_size=4)
    instance = pool.acquire("uid-1")
    assert isinstance(instance, FakePlugin)
    assert instance.uid == "uid-1"


def test_acquire_reuses_released_instance():
    pool = PluginPool(FakePlugin, max_size=4)
    first = pool.acquire("uid-1")
    pool.release(first)
    second = pool.acquire("uid-2")
    assert second is first
    assert second.uid == "uid-2"


def test_release_discards_when_pool_full():
    pool = PluginPool(FakePlugin, max_size=2)
    instances = [pool.acquire(f"uid-{i}") for i in range(3)]
    for inst in instances:
        pool.release(inst)
    # Pool should only hold max_size instances
    assert len(pool._pool) == 2


def test_release_calls_reset_request_state():
    pool = PluginPool(FakePlugin, max_size=4)
    inst = pool.acquire("uid-1")
    inst._reset_request_state = MagicMock()
    pool.release(inst)
    inst._reset_request_state.assert_called_once()


def test_acquire_calls_rebind_on_reuse():
    pool = PluginPool(FakePlugin, max_size=4)
    inst = pool.acquire("uid-1")
    inst._rebind = MagicMock()
    pool.release(inst)
    pool.acquire("uid-2")
    inst._rebind.assert_called_once_with("uid-2")


def test_concurrent_acquire_release_thread_safe():
    pool = PluginPool(FakePlugin, max_size=32)
    errors = []

    def worker(n):
        try:
            inst = pool.acquire(f"uid-{n}")
            pool.release(inst)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []


def test_no_infinite_recursion_on_create():
    """Pool creates new instances without recursion (uses object.__new__)."""
    pool = PluginPool(FakePlugin, max_size=4)
    # Should not raise RecursionError
    for i in range(5):
        inst = pool.acquire(f"uid-{i}")
        pool.release(inst)
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_plugin_pool.py -v
```
Expected: `ModuleNotFoundError: No module named 'flow_proxy_plugin.utils.plugin_pool'`

- [ ] **Step 3: Implement `PluginPool`**

```python
# flow_proxy_plugin/utils/plugin_pool.py
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
        new_instance.__init__(*args, **kwargs)  # type: ignore[misc]
        return new_instance

    def release(self, instance: T) -> None:
        """Return instance to pool after clearing per-request state."""
        instance._reset_request_state()  # type: ignore[attr-defined]
        with self._lock:
            if len(self._pool) < self.max_size:
                self._pool.append(instance)
        # If pool is full, instance is silently dropped for GC
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_plugin_pool.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add flow_proxy_plugin/utils/plugin_pool.py tests/test_plugin_pool.py
git commit -m "feat: add PluginPool generic instance pool"
```

---

## Chunk 3: BaseFlowProxyPlugin Refactor

### Task 3: Refactor `base_plugin.py` into a thin mixin

**Files:**
- Modify: `flow_proxy_plugin/plugins/base_plugin.py`

- [ ] **Step 1: Verify `HttpWebServerBasePlugin` connection close hook**

Before modifying any plugin code, check what lifecycle hooks are available:

```bash
python3 -c "
from proxy.http.server import HttpWebServerBasePlugin
import inspect
methods = [m for m in dir(HttpWebServerBasePlugin) if not m.startswith('__')]
print([m for m in methods if 'close' in m or 'disconnect' in m or 'destroy' in m or 'cleanup' in m])
"
```

Expected: should reveal whether `on_client_connection_close` exists. Note the result — it determines the release hook used in Task 4.

- [ ] **Step 2: Rewrite `base_plugin.py`**

Replace the entire file. Key changes:
- Remove `_setup_logging()` and `_initialize_components()`
- Add `_init_services()` (called once by concrete plugin `__init__`)
- Add `_rebind()` stub (overridden in each plugin with explicit parent class call)
- Add `_reset_request_state()` hook
- Add `_pooled` class variable guard
- Keep `_get_config_and_token()`, `_decode_bytes()`, `_extract_header_value()` unchanged

```python
# flow_proxy_plugin/plugins/base_plugin.py
"""Base plugin mixin providing shared services and utilities."""

from typing import Any

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
        config = self.load_balancer.get_next_config()
        config_name = config.get("name", config.get("clientId", "unknown"))

        try:
            jwt_token = self.jwt_generator.generate_token(config)
            return config, config_name, jwt_token

        except ValueError as e:
            self.logger.error(
                "Token generation failed for '%s': %s", config_name, str(e)
            )
            self.load_balancer.mark_config_failed(config)

            config = self.load_balancer.get_next_config()
            config_name = config.get("name", config.get("clientId", "unknown"))
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
```

- [ ] **Step 3: Run linters to check for issues**

```
make lint
```
Expected: PASS (no errors in modified file)

- [ ] **Step 4: Commit**

```bash
git add flow_proxy_plugin/plugins/base_plugin.py
git commit -m "refactor: simplify BaseFlowProxyPlugin to thin service mixin"
```

---

## Chunk 4: FlowProxyWebServerPlugin Pool Integration

### Task 4: Add pool to `FlowProxyWebServerPlugin`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

- [ ] **Step 1: Confirm connection close hook is available**

`on_client_connection_close` is confirmed present in `HttpWebServerBasePlugin`. Verify in the current environment before proceeding:

```bash
python3 -c "from proxy.http.server import HttpWebServerBasePlugin; print(hasattr(HttpWebServerBasePlugin, 'on_client_connection_close'))"
```
Expected output: `True`. If `False`, find the correct hook via `dir(HttpWebServerBasePlugin)` and use that instead throughout Task 4.

- [ ] **Step 2: Rewrite `web_server_plugin.py` `__init__` and add pool**

Add module-level pool (lazily initialized to avoid class-not-yet-defined issue at import time), `__new__` override, pool-aware `__init__`, `_rebind`, and release hook:

```python
# Add at module level (before class definition):
import os
import threading
from collections import deque
from typing import Optional

from ..utils.plugin_pool import PluginPool
from ..utils.process_services import ProcessServices

_web_pool: Optional["PluginPool[FlowProxyWebServerPlugin]"] = None
_web_pool_lock = threading.Lock()
_WEB_POOL_SIZE = int(os.getenv("FLOW_PROXY_PLUGIN_POOL_SIZE", "64"))
```

```python
# In class FlowProxyWebServerPlugin:

_log_once: bool = False  # Class variable: log init message only once

def __new__(
    cls,
    uid: str,
    flags: Any,
    client: Any,
    event_queue: Any,
    upstream_conn_pool: Any = None,
) -> "FlowProxyWebServerPlugin":
    global _web_pool
    if _web_pool is None:
        with _web_pool_lock:
            if _web_pool is None:
                _web_pool = PluginPool(cls, max_size=_WEB_POOL_SIZE)
    return _web_pool.acquire(uid, flags, client, event_queue, upstream_conn_pool)

def __init__(
    self,
    uid: str,
    flags: Any,
    client: Any,
    event_queue: Any,
    upstream_conn_pool: Any = None,
) -> None:
    if self._pooled:
        return  # Reused from pool: _rebind() already ran in pool.acquire()
    # First-time initialization (runs exactly once per instance)
    HttpWebServerBasePlugin.__init__(self, uid, flags, client, event_queue, upstream_conn_pool)
    self._init_services()
    self._pooled = True
    if not FlowProxyWebServerPlugin._log_once:
        FlowProxyWebServerPlugin._log_once = True
        self.logger.info("FlowProxyWebServerPlugin initialized (pooled)")

def _rebind(
    self,
    uid: str,
    flags: Any,
    client: Any,
    event_queue: Any,
    upstream_conn_pool: Any = None,
) -> None:
    """Rebind connection-specific proxy.py state for pool reuse."""
    HttpWebServerBasePlugin.__init__(self, uid, flags, client, event_queue, upstream_conn_pool)
    self._pooled = True  # ensure guard remains True after rebind

def on_client_connection_close(self) -> None:
    """Return instance to pool on connection close."""
    if _web_pool is not None and self._pooled:
        _web_pool.release(self)
```

- [ ] **Step 3: Update `handle_request` to use persistent `httpx.Client`**

Replace the `with httpx.Client() as client:` block with the process-level client from `ProcessServices`:

```python
# Before (in handle_request):
timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)
with httpx.Client() as client:
    with client.stream(...) as response:
        ...

# After:
http_client = ProcessServices.get().http_client
with http_client.stream(
    method=method,
    url=target_url,
    headers=headers,
    content=body,
    timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
) as response:
    ...
```

Note: `httpx.Client.stream()` is a context manager directly — no outer `with httpx.Client()` needed.

- [ ] **Step 4: Run tests**

```
pytest tests/test_web_server_plugin.py -v
```
Expected: some tests will fail because they mock `httpx.Client` at the wrong level. Note which ones — they'll be fixed in Chunk 6.

- [ ] **Step 5: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py
git commit -m "refactor: add PluginPool to FlowProxyWebServerPlugin"
```

---

## Chunk 5: FlowProxyPlugin Pool Integration

### Task 5: Add pool to `FlowProxyPlugin`

**Files:**
- Modify: `flow_proxy_plugin/plugins/proxy_plugin.py`

- [ ] **Step 1: Add pool to `FlowProxyPlugin`**

Same pattern as Task 4. Module-level `_proxy_pool`, `__new__`, `__init__` guard, `_rebind`, and release in `on_upstream_connection_close()`:

```python
# Module-level additions:
from proxy.http.proxy import HttpProxyBasePlugin
from ..utils.plugin_pool import PluginPool
from ..utils.process_services import ProcessServices

_proxy_pool: Optional["PluginPool[FlowProxyPlugin]"] = None
_proxy_pool_lock = threading.Lock()
_PROXY_POOL_SIZE = int(os.getenv("FLOW_PROXY_PLUGIN_POOL_SIZE", "64"))


# In class FlowProxyPlugin:

_log_once: bool = False

def __new__(cls, uid, flags, client, event_queue, upstream_conn_pool=None):
    global _proxy_pool
    if _proxy_pool is None:
        with _proxy_pool_lock:
            if _proxy_pool is None:
                _proxy_pool = PluginPool(cls, max_size=_PROXY_POOL_SIZE)
    return _proxy_pool.acquire(uid, flags, client, event_queue, upstream_conn_pool)

def __init__(self, uid, flags, client, event_queue, upstream_conn_pool=None):
    if self._pooled:
        return
    HttpProxyBasePlugin.__init__(self, uid, flags, client, event_queue, upstream_conn_pool)
    self._init_services()
    self._pooled = True
    if not FlowProxyPlugin._log_once:
        FlowProxyPlugin._log_once = True
        self.logger.info("FlowProxyPlugin initialized (pooled)")

def _rebind(self, uid, flags, client, event_queue, upstream_conn_pool=None):
    HttpProxyBasePlugin.__init__(self, uid, flags, client, event_queue, upstream_conn_pool)
    self._pooled = True  # ensure guard remains True after rebind

def on_upstream_connection_close(self) -> None:
    """Return instance to pool on upstream connection close."""
    self.logger.info("Upstream connection closed")
    if _proxy_pool is not None and self._pooled:
        _proxy_pool.release(self)
```

- [ ] **Step 2: Run tests**

```
pytest tests/test_plugin.py -v
```
Expected: some tests will fail due to mock scope changes — noted for Chunk 6.

- [ ] **Step 3: Commit**

```bash
git add flow_proxy_plugin/plugins/proxy_plugin.py
git commit -m "refactor: add PluginPool to FlowProxyPlugin"
```

---

## Chunk 6: Test Migration and Cleanup

### Task 6: Update `utils/__init__.py` and delete legacy files

**Files:**
- Modify: `flow_proxy_plugin/utils/__init__.py`
- Delete: `flow_proxy_plugin/utils/plugin_base.py`
- Delete: `flow_proxy_plugin/plugins/base.py` (legacy unused file)

- [ ] **Step 1: Update `utils/__init__.py`**

Remove `initialize_plugin_components` export, add `ProcessServices` and `PluginPool`:

```python
# flow_proxy_plugin/utils/__init__.py
"""Utility modules."""

from .log_filter import (
    BrokenPipeFilter,
    ProxyNoiseFilter,
    setup_proxy_log_filters,
)
from .logging import ColoredFormatter, Colors, setup_colored_logger, setup_logging
from .plugin_pool import PluginPool
from .process_services import ProcessServices

__all__ = [
    "BrokenPipeFilter",
    "ProxyNoiseFilter",
    "setup_proxy_log_filters",
    "Colors",
    "ColoredFormatter",
    "setup_colored_logger",
    "setup_logging",
    "PluginPool",
    "ProcessServices",
]
```

- [ ] **Step 2: Delete legacy files**

```bash
git rm flow_proxy_plugin/utils/plugin_base.py
git rm flow_proxy_plugin/plugins/base.py
```

- [ ] **Step 3: Run full test suite to see what's broken**

```
pytest -v 2>&1 | head -80
```

Note all failing tests.

- [ ] **Step 4: Commit deletions**

```bash
git add flow_proxy_plugin/utils/__init__.py
git commit -m "chore: remove plugin_base.py and legacy base.py, update utils exports"
```

---

### Task 7: Migrate `test_shared_state.py`

**Files:**
- Modify: `tests/test_shared_state.py`

Replace all `SharedComponentManager` / `initialize_plugin_components` usage with `ProcessServices`.

- [ ] **Step 1: Rewrite `test_shared_state.py`**

```python
# tests/test_shared_state.py
"""Tests for shared state across plugin instances via ProcessServices."""

import json
import os

import pytest

from flow_proxy_plugin.utils.process_services import ProcessServices


@pytest.fixture(autouse=True)
def reset_services(tmp_path):
    ProcessServices.reset()
    secrets = [
        {"name": "config1", "clientId": "id1", "clientSecret": "s1", "tenant": "t1"},
        {"name": "config2", "clientId": "id2", "clientSecret": "s2", "tenant": "t2"},
    ]
    f = tmp_path / "secrets.json"
    f.write_text(json.dumps(secrets))
    os.environ["FLOW_PROXY_SECRETS_FILE"] = str(f)
    yield
    ProcessServices.reset()
    os.environ.pop("FLOW_PROXY_SECRETS_FILE", None)


def _get():
    from unittest.mock import patch
    with patch("flow_proxy_plugin.utils.process_services.setup_colored_logger"):
        with patch("flow_proxy_plugin.utils.process_services.setup_file_handler_for_child_process"):
            with patch("flow_proxy_plugin.utils.process_services.setup_proxy_log_filters"):
                return ProcessServices.get()


class TestSharedState:

    def test_shared_load_balancer_across_calls(self):
        """LoadBalancer is the same instance across multiple ProcessServices.get() calls."""
        svc1 = _get()
        svc2 = _get()
        assert svc1.load_balancer is svc2.load_balancer

    def test_round_robin_state_is_shared(self):
        """Round-robin state advances globally, not per-caller."""
        svc = _get()
        c1 = svc.load_balancer.get_next_config()
        c2 = svc.load_balancer.get_next_config()
        assert c1["name"] == "config1"
        assert c2["name"] == "config2"

    def test_jwt_generator_is_shared(self):
        """JWTGenerator is the same singleton instance across calls.

        Note: This is a behavioral inversion from the old SharedComponentManager design
        where each plugin instance got an independent JWTGenerator. ProcessServices
        intentionally shares a single instance — JWT caching is already class-level
        so independent instances were always functionally equivalent anyway.
        """
        svc1 = _get()
        svc2 = _get()
        assert svc1.jwt_generator is svc2.jwt_generator
```

- [ ] **Step 2: Run tests**

```
pytest tests/test_shared_state.py -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_shared_state.py
git commit -m "test: migrate test_shared_state to ProcessServices"
```

---

### Task 8: Fix `test_plugin.py` and `test_web_server_plugin.py`

**Files:**
- Modify: `tests/test_plugin.py`
- Modify: `tests/test_web_server_plugin.py`

The key changes needed:
1. Replace `SharedComponentManager().reset()` with `ProcessServices.reset()`
2. Mock at `ProcessServices` level instead of `SecretsManager.load_secrets`
3. `httpx.Client` mock target changes from `httpx.Client` to `flow_proxy_plugin.utils.process_services.ProcessServices.get().http_client`

- [ ] **Step 1: Update `test_web_server_plugin.py` fixtures**

Change the `plugin` fixture and `mock_httpx_stream` helper. The existing file has a module-level `mock_httpx_stream` context manager that patches `httpx.Client` — this must change to mock `mock_svc.http_client.stream` directly.

**a) Replace the `plugin` fixture:**

```python
# Before:
from flow_proxy_plugin.utils.plugin_base import SharedComponentManager
SharedComponentManager().reset()
with patch("flow_proxy_plugin.core.config.SecretsManager.load_secrets") as mock_load:
    mock_load.return_value = [...]
    return FlowProxyWebServerPlugin(**mock_plugin_args)

# After:
import flow_proxy_plugin.plugins.web_server_plugin as ws_mod
from flow_proxy_plugin.utils.process_services import ProcessServices

@pytest.fixture
def mock_svc() -> MagicMock:
    """Shared ProcessServices mock reused across fixtures and tests."""
    svc = MagicMock()
    svc.configs = [
        {"name": "test-config-1", "clientId": "test-client-1",
         "clientSecret": "test-secret-1", "tenant": "test-tenant-1"},
    ]
    svc.load_balancer = MagicMock()
    svc.jwt_generator = MagicMock()
    svc.request_forwarder = MagicMock()
    svc.request_forwarder.target_base_url = "https://flow.ciandt.com"
    svc.request_forwarder.validate_request.return_value = True
    svc.request_filter = MagicMock()
    svc.http_client = MagicMock()
    return svc


@pytest.fixture(autouse=True)
def reset_state(mock_svc: MagicMock):
    """Reset ProcessServices singleton and pool module vars before/after each test."""
    ProcessServices.reset()
    ws_mod._web_pool = None
    yield
    ProcessServices.reset()
    ws_mod._web_pool = None


@pytest.fixture
def plugin(mock_plugin_args: dict, mock_svc: MagicMock) -> FlowProxyWebServerPlugin:
    with patch.object(ProcessServices, "get", return_value=mock_svc):
        yield FlowProxyWebServerPlugin(**mock_plugin_args)
```

**b) Replace `mock_httpx_stream` to mock on `mock_svc.http_client.stream`:**

```python
# Before (existing pattern — will break after refactor):
@contextmanager
def mock_httpx_stream(mock_response_lines, status_code=200):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.headers = {}
    mock_response.__iter__ = MagicMock(return_value=iter(mock_response_lines))
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_client = MagicMock()
    mock_client.stream.return_value = mock_response
    with patch("httpx.Client", return_value=mock_client):
        yield mock_response

# After — no longer patches httpx.Client constructor; directly mocks the
# persistent http_client.stream that the plugin gets from ProcessServices:
@contextmanager
def mock_httpx_stream(mock_svc: MagicMock, mock_response_lines, status_code=200):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.headers = {}
    mock_response.__iter__ = MagicMock(return_value=iter(mock_response_lines))
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_svc.http_client.stream.return_value = mock_response
    yield mock_response
```

All tests that previously called `mock_httpx_stream(lines)` must be updated to
`mock_httpx_stream(mock_svc, lines)` — passing the `mock_svc` fixture parameter.

**Note**: The `mock_svc` fixture must be declared as a parameter in every test function
or class that uses `mock_httpx_stream` after this change.

- [ ] **Step 2: Update `test_handle_request_timeout_configuration`**

This test verifies timeout config. After refactor, the `httpx.Timeout` is configured on `ProcessServices.http_client` (at init time) not per-request. Update the test to verify `ProcessServices.http_client` was created with the right timeout, or remove this test if timeout is now tested in `test_process_services.py`.

- [ ] **Step 3: Update `test_plugin.py`**

`test_plugin.py` has a different fixture structure (no httpx streaming). Apply these targeted changes:

**a) Replace the reset call in each test that uses `SharedComponentManager().reset()`:**
```python
# Before (in TestFlowProxyPluginInitialization and any other tests):
from flow_proxy_plugin.utils.plugin_base import SharedComponentManager
SharedComponentManager().reset()

# After:
from flow_proxy_plugin.utils.process_services import ProcessServices
ProcessServices.reset()
```

**b) Update the `plugin` fixture (`mock_plugin_args`) — wrap instantiation to mock `ProcessServices.get()` and reset pool module var:**
```python
import flow_proxy_plugin.plugins.proxy_plugin as proxy_mod
from flow_proxy_plugin.utils.process_services import ProcessServices

@pytest.fixture(autouse=True)
def reset_state():
    """Reset ProcessServices singleton and proxy pool module var before/after each test."""
    ProcessServices.reset()
    proxy_mod._proxy_pool = None
    yield
    ProcessServices.reset()
    proxy_mod._proxy_pool = None


@pytest.fixture
def plugin(mock_plugin_args: dict[str, Any]) -> FlowProxyPlugin:
    from unittest.mock import MagicMock, patch
    mock_svc = MagicMock()
    mock_svc.configs = [
        {"name": "test-config-1", "clientId": "test-client-1",
         "clientSecret": "test-secret-1", "tenant": "test-tenant-1"},
        {"name": "test-config-2", "clientId": "test-client-2",
         "clientSecret": "test-secret-2", "tenant": "test-tenant-2"},
    ]
    mock_svc.load_balancer = MagicMock()
    mock_svc.jwt_generator = MagicMock()
    mock_svc.request_forwarder = MagicMock()
    mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"
    mock_svc.request_forwarder.validate_request.return_value = True
    mock_svc.request_forwarder.modify_request_headers.side_effect = lambda req, *a: req
    mock_svc.request_forwarder.handle_response_chunk.side_effect = lambda c: c
    mock_svc.request_filter = MagicMock()
    with patch.object(ProcessServices, "get", return_value=mock_svc):
        yield FlowProxyPlugin(**mock_plugin_args)
```

**c) `test_plugin_initialization_success`**: Same pattern as fixture — patch `ProcessServices.get` to return a fully configured `mock_svc`. Assert `plugin is not None`.

**d) `test_plugin_initialization_failure`**: `FileNotFoundError` now surfaces through `ProcessServices._initialize()`. Test it by letting real initialization run with a missing secrets file:

```python
def test_plugin_initialization_failure(
    self, mock_plugin_args: dict[str, Any]
) -> None:
    """Plugin instantiation raises when ProcessServices initialization fails."""
    from flow_proxy_plugin.utils.process_services import ProcessServices
    ProcessServices.reset()

    with patch(
        "flow_proxy_plugin.core.config.SecretsManager.load_secrets",
        side_effect=FileNotFoundError("Secrets file not found"),
    ):
        with patch("flow_proxy_plugin.utils.process_services.setup_colored_logger"):
            with patch("flow_proxy_plugin.utils.process_services.setup_file_handler_for_child_process"):
                with patch("flow_proxy_plugin.utils.process_services.setup_proxy_log_filters"):
                    with pytest.raises(FileNotFoundError):
                        FlowProxyPlugin(**mock_plugin_args)

    ProcessServices.reset()
```

- [ ] **Step 4: Run full test suite**

```
pytest -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_plugin.py tests/test_web_server_plugin.py
git commit -m "test: update plugin tests for ProcessServices and PluginPool"
```

---

### Task 9: Update `test_thread_safety.py`

**Files:**
- Modify: `tests/test_thread_safety.py`

- [ ] **Step 1: Add pool concurrency tests**

Read the existing `tests/test_thread_safety.py` first. It already has `import threading` at the top — do not duplicate it. Append the following pool-specific tests:

```python
# Append to tests/test_thread_safety.py
# Note: `import threading` already exists in the file — do not add again.

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from flow_proxy_plugin.utils.plugin_pool import PluginPool
from flow_proxy_plugin.utils.process_services import ProcessServices


class FakePoolPlugin:
    """Minimal plugin for pool thread-safety tests."""
    _pooled: bool = False

    def __init__(self, uid: str) -> None:
        self.uid = uid
        self._pooled = True

    def _rebind(self, uid: str) -> None:
        self.uid = uid

    def _reset_request_state(self) -> None:
        pass


class TestPluginPoolThreadSafety:
    """Concurrent acquire/release does not corrupt pool state."""

    def test_concurrent_acquire_all_instances_valid(self) -> None:
        """50 concurrent threads each get a valid FakePoolPlugin instance."""
        pool = PluginPool(FakePoolPlugin, max_size=64)
        results: list[FakePoolPlugin] = []
        lock = threading.Lock()

        def worker(n: int) -> None:
            inst = pool.acquire(f"uid-{n}")
            with lock:
                results.append(inst)
            pool.release(inst)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        assert all(isinstance(r, FakePoolPlugin) for r in results)

    def test_pool_size_never_exceeds_max(self) -> None:
        """Pool size stays at or below max_size under concurrent load."""
        pool = PluginPool(FakePoolPlugin, max_size=8)
        instances = [pool.acquire(f"uid-{i}") for i in range(20)]
        for inst in instances:
            pool.release(inst)
        assert len(pool._pool) <= 8
```

- [ ] **Step 2: Run tests**

```
pytest tests/test_thread_safety.py -v
```
Expected: all tests PASS (including new pool tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_thread_safety.py
git commit -m "test: add PluginPool thread-safety tests"
```

---

### Task 10: Add `httpx.Client` resilience to `ProcessServices`

**Files:**
- Modify: `flow_proxy_plugin/utils/process_services.py`
- Modify: `tests/test_process_services.py`

If the persistent `httpx.Client` encounters a `TransportError` that corrupts its state, subsequent requests will all fail until process restart. Add a `get_http_client()` method that detects and atomically rebuilds a broken client.

- [ ] **Step 1: Add failing test for client resilience**

Append to `tests/test_process_services.py`:

```python
def test_get_http_client_rebuilds_after_mark_dirty():
    svc = _make_services()
    original = svc.http_client
    svc.mark_http_client_dirty()
    new_client = svc.get_http_client()
    assert new_client is not original
    assert svc.http_client is new_client


def test_get_http_client_returns_same_when_healthy():
    svc = _make_services()
    c1 = svc.get_http_client()
    c2 = svc.get_http_client()
    assert c1 is c2
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_process_services.py::test_get_http_client_rebuilds_after_mark_dirty -v
```
Expected: `AttributeError: 'ProcessServices' object has no attribute 'mark_http_client_dirty'`

- [ ] **Step 3: Add resilience methods to `ProcessServices`**

Add after `_initialize()`:

```python
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
        self.http_client.close()
    except Exception:
        pass
    self.http_client = None  # type: ignore[assignment]
```

- [ ] **Step 4: Update `handle_request` in `web_server_plugin.py` to use `get_http_client()`**

```python
# Before:
http_client = ProcessServices.get().http_client

# After:
http_client = ProcessServices.get().get_http_client()
```

Also catch `httpx.TransportError` in `handle_request` to mark the client dirty:

```python
except httpx.TransportError as e:
    self.logger.error("Transport error — marking httpx client dirty: %s", e)
    ProcessServices.get().mark_http_client_dirty()
    self._send_error(503, "Upstream transport error")
```

- [ ] **Step 5: Run tests**

```
pytest tests/test_process_services.py -v
```
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/utils/process_services.py flow_proxy_plugin/plugins/web_server_plugin.py tests/test_process_services.py
git commit -m "feat: add httpx.Client resilience with dirty rebuild to ProcessServices"
```

---

### Task 11: Final verification

- [ ] **Step 1: Run linters**

```
make lint
```
Expected: PASS

- [ ] **Step 2: Run full test suite with coverage**

```
make test-cov
```
Expected: all tests PASS, coverage maintained or improved

- [ ] **Step 3: Smoke test — start the service and verify no repeated init logs**

```bash
FLOW_PROXY_LOG_LEVEL=DEBUG poetry run flow-proxy --port 8899 &
PID=$!
sleep 2
# Send 3 requests via reverse proxy
curl -s http://localhost:8899/v1/models
curl -s http://localhost:8899/v1/models
curl -s http://localhost:8899/v1/models
kill $PID
```

Verify in logs: `FlowProxyWebServerPlugin initialized (pooled)` appears EXACTLY ONCE. `ProcessServices initialized` appears EXACTLY ONCE per process. No `Initializing FlowProxyWebServerPlugin...` entries.

- [ ] **Step 4: Final commit**

```bash
git add -u
git commit -m "chore: complete performance optimization refactoring"
```

---

## File Change Summary

| File | Action |
|------|--------|
| `flow_proxy_plugin/utils/process_services.py` | **New** |
| `flow_proxy_plugin/utils/plugin_pool.py` | **New** |
| `flow_proxy_plugin/utils/plugin_base.py` | **Deleted** |
| `flow_proxy_plugin/plugins/base.py` | **Deleted** (legacy) |
| `flow_proxy_plugin/plugins/base_plugin.py` | **Rewritten** |
| `flow_proxy_plugin/plugins/web_server_plugin.py` | **Modified** |
| `flow_proxy_plugin/plugins/proxy_plugin.py` | **Modified** |
| `flow_proxy_plugin/utils/__init__.py` | **Modified** |
| `tests/test_process_services.py` | **New** |
| `tests/test_plugin_pool.py` | **New** |
| `tests/test_shared_state.py` | **Rewritten** |
| `tests/test_plugin.py` | **Modified** |
| `tests/test_web_server_plugin.py` | **Modified** |
| `tests/test_thread_safety.py` | **Modified** |

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| `HttpWebServerBasePlugin` has no `on_client_connection_close` | Verify in Task 3 Step 1; use correct hook name |
| `httpx.Client` not thread-safe for concurrent stream | httpx docs confirm Client is thread-safe; streams are independent |
| Pool instances hold stale references after `ProcessServices.reset()` (tests) | Each test file that instantiates plugin classes must include an `autouse` fixture that resets both the singleton and the module-level pool var: `ProcessServices.reset(); proxy_mod._proxy_pool = None` (or `ws_mod._web_pool = None`). See Task 8 Step 1 and Step 3b for concrete fixture code. |
| `_pooled` class var vs instance var confusion | Class var `= False` is default; instance var set to `True` after first init — Python attribute lookup picks instance var first |
