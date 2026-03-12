# Log Grouping Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `[req_id][COMPONENT]` prefixes to all log lines so concurrent requests can be traced in isolation and component call order is visible.

**Architecture:** A `threading.local` store holds `(req_id, component)` for the current thread. A `logging.Filter` attached to the `flow_proxy` logger prepends `[req_id][COMPONENT]` to every log message. Request entry points set context in a `try/finally` block; sub-components temporarily override the component tag via a `component_context` context manager.

**Tech Stack:** Python 3.12+, `threading.local`, `secrets.token_hex`, `logging.Filter`, `contextlib.contextmanager`

**Spec:** `docs/superpowers/specs/2026-03-12-log-grouping-design.md`

---

## Chunk 1: log_context module + RequestContextFilter

### Task 1: `log_context.py` — write failing tests

**Files:**
- Create: `tests/test_log_context.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for log_context utilities."""

import threading

import pytest

from flow_proxy_plugin.utils.log_context import (
    clear_request_context,
    component_context,
    get_request_prefix,
    set_request_context,
)


class TestGetRequestPrefix:
    def test_returns_empty_when_no_context(self) -> None:
        clear_request_context()
        assert get_request_prefix() == ""

    def test_returns_prefix_with_req_id_and_component(self) -> None:
        set_request_context("a1b2c3", "WS")
        assert get_request_prefix() == "[a1b2c3][WS] "
        clear_request_context()

    def test_returns_prefix_without_component(self) -> None:
        set_request_context("a1b2c3", "")
        assert get_request_prefix() == "[a1b2c3] "
        clear_request_context()

    def test_clear_resets_prefix(self) -> None:
        set_request_context("a1b2c3", "WS")
        clear_request_context()
        assert get_request_prefix() == ""


class TestComponentContext:
    def test_overrides_component_temporarily(self) -> None:
        set_request_context("abc123", "WS")
        with component_context("JWT"):
            assert get_request_prefix() == "[abc123][JWT] "
        assert get_request_prefix() == "[abc123][WS] "
        clear_request_context()

    def test_restores_component_on_exception(self) -> None:
        set_request_context("abc123", "WS")
        try:
            with component_context("JWT"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert get_request_prefix() == "[abc123][WS] "
        clear_request_context()

    def test_nesting(self) -> None:
        set_request_context("abc123", "WS")
        with component_context("LB"):
            assert get_request_prefix() == "[abc123][LB] "
            with component_context("JWT"):
                assert get_request_prefix() == "[abc123][JWT] "
            assert get_request_prefix() == "[abc123][LB] "
        assert get_request_prefix() == "[abc123][WS] "
        clear_request_context()

    def test_no_req_id_returns_empty(self) -> None:
        clear_request_context()
        with component_context("JWT"):
            assert get_request_prefix() == ""


class TestThreadIsolation:
    def test_contexts_do_not_bleed_across_threads(self) -> None:
        results: dict[str, str] = {}

        def thread_a() -> None:
            set_request_context("aaaaaa", "WS")
            threading.Event().wait(0.05)  # let thread_b run
            results["a"] = get_request_prefix()
            clear_request_context()

        def thread_b() -> None:
            set_request_context("bbbbbb", "PROXY")
            results["b"] = get_request_prefix()
            clear_request_context()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        assert results["a"] == "[aaaaaa][WS] "
        assert results["b"] == "[bbbbbb][PROXY] "
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_log_context.py -v
```

Expected: `ImportError` — `flow_proxy_plugin.utils.log_context` does not exist yet.

---

### Task 2: Implement `log_context.py`

**Files:**
- Create: `flow_proxy_plugin/utils/log_context.py`

- [ ] **Step 1: Write the implementation**

```python
"""Thread-local request context for log grouping."""

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
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest tests/test_log_context.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add flow_proxy_plugin/utils/log_context.py tests/test_log_context.py
git commit -m "feat: add log_context module for thread-local request context"
```

---

### Task 3: `RequestContextFilter` — write failing tests

**Files:**
- Modify: `tests/test_logging.py`

- [ ] **Step 1: Add failing tests to `tests/test_logging.py`**

Add this class at the end of the file:

```python
class TestRequestContextFilter:
    """Tests for RequestContextFilter."""

    def setup_method(self) -> None:
        from flow_proxy_plugin.utils.log_context import clear_request_context
        clear_request_context()

    def teardown_method(self) -> None:
        from flow_proxy_plugin.utils.log_context import clear_request_context
        clear_request_context()

    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="flow_proxy",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_prepends_prefix_when_context_set(self) -> None:
        from flow_proxy_plugin.utils.log_context import set_request_context
        from flow_proxy_plugin.utils.logging import RequestContextFilter

        set_request_context("a1b2c3", "WS")
        f = RequestContextFilter()
        record = self._make_record("hello world")
        f.filter(record)
        assert record.msg == "[a1b2c3][WS] hello world"

    def test_no_op_when_no_context(self) -> None:
        from flow_proxy_plugin.utils.logging import RequestContextFilter

        f = RequestContextFilter()
        record = self._make_record("hello world")
        f.filter(record)
        assert record.msg == "hello world"

    def test_filter_always_returns_true(self) -> None:
        from flow_proxy_plugin.utils.logging import RequestContextFilter

        f = RequestContextFilter()
        record = self._make_record("msg")
        assert f.filter(record) is True

    def test_filter_attached_at_logger_level(self) -> None:
        """RequestContextFilter must be on the logger, not individual handlers."""
        from flow_proxy_plugin.utils.log_context import set_request_context
        from flow_proxy_plugin.utils.logging import RequestContextFilter, setup_colored_logger

        logger = logging.getLogger("test.filter.attachment")
        logger.handlers.clear()
        logger.filters.clear()
        set_request_context("x1y2z3", "WS")
        setup_colored_logger(logger)

        # Filter must be on the logger object
        assert any(isinstance(f, RequestContextFilter) for f in logger.filters)

    def test_no_double_prefix_with_multiple_handlers(self) -> None:
        """A single logger.info() call with two handlers should produce prefix exactly once."""
        import io
        from flow_proxy_plugin.utils.log_context import set_request_context
        from flow_proxy_plugin.utils.logging import RequestContextFilter, setup_colored_logger

        logger = logging.getLogger("test.no.double.prefix")
        logger.handlers.clear()
        logger.filters.clear()
        setup_colored_logger(logger)

        # Add a second handler
        stream = io.StringIO()
        second_handler = logging.StreamHandler(stream)
        second_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(second_handler)

        set_request_context("ab12cd", "WS")
        logger.info("test message")

        output = stream.getvalue()
        # Prefix should appear exactly once, not twice
        assert output.count("[ab12cd][WS]") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_logging.py::TestRequestContextFilter -v
```

Expected: `ImportError` — `RequestContextFilter` does not exist yet.

---

### Task 4: Implement `RequestContextFilter` in `logging.py`

**Files:**
- Modify: `flow_proxy_plugin/utils/logging.py`

- [ ] **Step 1: Add import at top of `logging.py`**

After the existing imports, add:

```python
from .log_context import get_request_prefix
```

- [ ] **Step 2: Add `RequestContextFilter` class after `ColoredFormatter`**

Insert after the `ColoredFormatter` class (after line 41):

```python
class RequestContextFilter(logging.Filter):
    """Prepends [req_id][COMPONENT] prefix to log messages when request context is set.

    Attached to the logger object (not individual handlers) so filter() is called
    exactly once per log call — preventing double-prefix when multiple handlers exist.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Prepend request context prefix to record.msg."""
        prefix = get_request_prefix()
        if prefix:
            record.msg = f"{prefix}{record.msg}"
        return True
```

- [ ] **Step 3: Attach filter in `setup_colored_logger()`**

In `setup_colored_logger()`, replace `logger.handlers.clear()` with:

```python
    logger.handlers.clear()
    logger.filters.clear()  # reset filters to match handler reset semantics
```

Then, before the console handler is added, insert:

```python
    # Attach request context filter at logger level (not handler level) to avoid
    # double-prefix when multiple handlers are present.
    logger.addFilter(RequestContextFilter())
```

(No duplicate guard needed — `logger.filters.clear()` above ensures a clean state on every call.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_logging.py::TestRequestContextFilter -v
```

Expected: All tests PASS.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
pytest tests/test_logging.py -v
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/utils/logging.py tests/test_logging.py
git commit -m "feat: add RequestContextFilter for [req_id][COMPONENT] log prefixes"
```

---

## Chunk 2: Plugin entry/exit points

### Task 5: Web server plugin — set/clear context in `handle_request()`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

- [ ] **Step 1: Add imports at top of `web_server_plugin.py`**

After the existing imports, add:

```python
import secrets

from ..utils.log_context import clear_request_context, set_request_context
```

- [ ] **Step 2: Wrap `handle_request()` body in try/finally**

Replace the opening of `handle_request()` (lines 90–95 in current file):

```python
    def handle_request(self, request: HttpParser) -> None:
        """Handle web server request."""
        method = self._decode_bytes(request.method) if request.method else "GET"
        path = self._decode_bytes(request.path) if request.path else "/"

        self.logger.info("→ %s %s", method, path)

        try:
            _, config_name, jwt_token = self._get_config_and_token()
```

with:

```python
    def handle_request(self, request: HttpParser) -> None:
        """Handle web server request."""
        method = self._decode_bytes(request.method) if request.method else "GET"
        path = self._decode_bytes(request.path) if request.path else "/"

        req_id = secrets.token_hex(3)
        set_request_context(req_id, "WS")
        try:
            self.logger.info("→ %s %s", method, path)

            _, config_name, jwt_token = self._get_config_and_token()
```

And at the very end of `handle_request()`, add a `finally` block. The current method ends with the last `except Exception` block (around line 163). Change the structure so the outermost `try` that wraps the entire method body has a matching `finally`:

The full updated method signature and structure:

```python
    def handle_request(self, request: HttpParser) -> None:
        """Handle web server request."""
        method = self._decode_bytes(request.method) if request.method else "GET"
        path = self._decode_bytes(request.path) if request.path else "/"

        req_id = secrets.token_hex(3)
        set_request_context(req_id, "WS")
        try:
            self.logger.info("→ %s %s", method, path)

            try:
                _, config_name, jwt_token = self._get_config_and_token()
                # ... rest of existing try body unchanged ...
            except (BrokenPipeError, ConnectionResetError) as e:
                self.logger.debug("Client disconnected (%s)", type(e).__name__)
            except httpx.RemoteProtocolError as e:
                self.logger.error(
                    "Backend streaming failed (RemoteProtocolError): %s",
                    str(e),
                    exc_info=True,
                )
            except httpx.TransportError as e:
                self.logger.error("Transport error — marking httpx client dirty: %s", e)
                ProcessServices.get().mark_http_client_dirty()
                self._send_error(503, "Upstream transport error")
            except Exception as e:
                self.logger.error("✗ Request failed: %s", str(e), exc_info=True)
                self._send_error()
        finally:
            clear_request_context()
```

- [ ] **Step 3: Run existing web server plugin tests**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: All tests PASS. Context is cleared in `finally` so no test interference.

- [ ] **Step 4: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py
git commit -m "feat: set request context in web server plugin handle_request"
```

---

### Task 6: Forward proxy plugin — set/clear context in `before_upstream_connection()`

**Files:**
- Modify: `flow_proxy_plugin/plugins/proxy_plugin.py`

- [ ] **Step 1: Add imports at top of `proxy_plugin.py`**

After the existing imports, add:

```python
import secrets

from ..utils.log_context import clear_request_context, set_request_context
```

- [ ] **Step 2: Wrap `before_upstream_connection()` body in try/finally**

Replace the opening of `before_upstream_connection()`:

```python
    def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
        ...
        try:
            # Convert reverse proxy requests to forward proxy format
            self._convert_reverse_proxy_request(request)
```

with:

```python
    def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
        ...
        req_id = secrets.token_hex(3)
        set_request_context(req_id, "PROXY")
        try:
            # Convert reverse proxy requests to forward proxy format
            self._convert_reverse_proxy_request(request)
```

Add `finally: clear_request_context()` after the last `except` block.

Full updated structure:

```python
    def before_upstream_connection(self, request: HttpParser) -> HttpParser | None:
        """...(keep existing docstring)..."""
        req_id = secrets.token_hex(3)
        set_request_context(req_id, "PROXY")
        try:
            # Convert reverse proxy requests to forward proxy format
            self._convert_reverse_proxy_request(request)

            # Validate request
            if not self.request_forwarder.validate_request(request):
                self.logger.error("Request validation failed")
                return None

            # Get config and token with failover
            _, config_name, jwt_token = self._get_config_and_token()

            # Modify request headers
            modified_request = self.request_forwarder.modify_request_headers(
                request, jwt_token, config_name
            )

            # Log success
            target_url = self._decode_bytes(request.path) if request.path else "unknown"
            self.logger.info(
                "Request processed with config '%s' → %s", config_name, target_url
            )

            return modified_request

        except (RuntimeError, ValueError) as e:
            self.logger.error("Request processing failed: %s", str(e))
            return None
        except Exception as e:
            self.logger.error("Unexpected error: %s", str(e), exc_info=True)
            return None
        finally:
            clear_request_context()
```

- [ ] **Step 3: Run existing forward proxy plugin tests**

```bash
pytest tests/test_plugin.py -v
```

Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add flow_proxy_plugin/plugins/proxy_plugin.py
git commit -m "feat: set request context in forward proxy plugin before_upstream_connection"
```

---

## Chunk 3: Sub-component component tags

### Task 7: `base_plugin.py` — LB and JWT tags in `_get_config_and_token()`

**Files:**
- Modify: `flow_proxy_plugin/plugins/base_plugin.py`

- [ ] **Step 1: Add import**

After the existing import of `ProcessServices`, add:

```python
from ..utils.log_context import component_context
```

- [ ] **Step 2: Replace `_get_config_and_token()` body**

Replace the current implementation with:

```python
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
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_plugin.py tests/test_web_server_plugin.py tests/test_shared_state.py -v
```

Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add flow_proxy_plugin/plugins/base_plugin.py
git commit -m "feat: tag LB and JWT component context in _get_config_and_token"
```

---

### Task 8: `web_server_plugin.py` — FILTER and FWD tags in `handle_request()`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

- [ ] **Step 1: Add `component_context` import**

In the existing import from `..utils.log_context`, add `component_context`:

```python
from ..utils.log_context import clear_request_context, component_context, set_request_context
```

- [ ] **Step 2: Wrap filter and forwarder operations**

Inside `handle_request()`, locate the section after `_get_config_and_token()` returns:

```python
            # Build request params
            filter_rule = self.request_filter.find_matching_rule(request, path)
            if filter_rule:
                path = self.request_filter.filter_query_params(
                    path, filter_rule.query_params_to_remove
                )

            target_url = f"{self.request_forwarder.target_base_url}{path}"
            headers = self._build_headers(request, jwt_token, filter_rule)
            body = self._get_request_body(request, filter_rule)

            if self.logger.isEnabledFor(logging.DEBUG):
                self._log_request_details(method, path, target_url, headers, body)

            self.logger.info("Sending request to backend: %s", target_url)
```

Replace with:

```python
            # Build request params
            with component_context("FILTER"):
                filter_rule = self.request_filter.find_matching_rule(request, path)
                if filter_rule:
                    path = self.request_filter.filter_query_params(
                        path, filter_rule.query_params_to_remove
                    )

            target_url = f"{self.request_forwarder.target_base_url}{path}"
            headers = self._build_headers(request, jwt_token, filter_rule)
            body = self._get_request_body(request, filter_rule)

            if self.logger.isEnabledFor(logging.DEBUG):
                self._log_request_details(method, path, target_url, headers, body)

            with component_context("FWD"):
                self.logger.info("Sending request to backend: %s", target_url)
            # http_client.stream(...) block follows here, outside FWD context (reverts to WS)
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py
git commit -m "feat: tag FILTER and FWD component context in web server handle_request"
```

---

## Chunk 4: Export and final verification

### Task 9: Export `log_context` from utils `__init__.py`

**Files:**
- Modify: `flow_proxy_plugin/utils/__init__.py`

- [ ] **Step 1: Add exports**

In the existing `from .logging import ...` line, add `RequestContextFilter`:

```python
from .logging import ColoredFormatter, Colors, RequestContextFilter, setup_colored_logger, setup_logging
```

Add `log_context` imports:

```python
from .log_context import (
    clear_request_context,
    component_context,
    get_request_prefix,
    set_request_context,
)
```

And add all five names to `__all__`:

```python
    "RequestContextFilter",
    "clear_request_context",
    "component_context",
    "get_request_prefix",
    "set_request_context",
```

- [ ] **Step 2: Run full test suite**

```bash
pytest -v
```

Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add flow_proxy_plugin/utils/__init__.py
git commit -m "chore: export log_context from utils __init__"
```

---

### Task 10: Final verification — run linters and full test suite

- [ ] **Step 1: Run linters**

```bash
make lint
```

Expected: No errors. If mypy reports issues with `component_context` return type, the `Iterator[None]` import must be from `collections.abc`.

- [ ] **Step 2: Run all tests with coverage**

```bash
make test-cov
```

Expected: All tests PASS.

- [ ] **Step 3: Manual smoke check (optional)**

Start the proxy and send a request:

```bash
make run &
curl -s http://localhost:8899/v1/messages -H "anthropic-version: 2023-06-01" -d '{"model":"claude-3-haiku-20240307","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}' | head -5
```

Check `logs/flow_proxy_plugin.log` — each line within a request should carry `[xxxxxx][WS]`, `[xxxxxx][LB]`, etc. prefix.

- [ ] **Step 4: Final commit if any cleanup needed**

```bash
git add -p
git commit -m "chore: final cleanup for log grouping feature"
```
