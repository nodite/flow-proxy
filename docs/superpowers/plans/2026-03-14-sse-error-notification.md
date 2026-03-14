# SSE Mid-Stream Error Notification Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the backend drops a chunked SSE connection mid-stream after response headers have already been sent, inject a synthetic SSE `event: error` item so the client receives a structured error instead of a silent stream termination.

**Architecture:** Add `is_sse: bool = False` to `StreamingState`; propagate it from `_ResponseHeaders` in `read_from_descriptors`; add an `elif state.is_sse` branch in `_finish_stream`; implement `_send_sse_error_event()` to queue the error event bytes from the main thread.

**Tech Stack:** Python 3.12, httpx, proxy.py, pytest

**Spec:** `docs/superpowers/specs/2026-03-14-sse-error-notification-design.md`

---

## Chunk 1: Implementation + Tests

### Task 1: Add `import json` at module level

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:1-12`

- [ ] **Step 1: Add `import json` to module imports**

Open `flow_proxy_plugin/plugins/web_server_plugin.py`. The current imports block starts with:

```python
"""Web Server plugin for reverse proxy mode."""

import logging
import os
import queue
import secrets
import threading
```

Add `import json` in alphabetical order among the stdlib imports:

```python
"""Web Server plugin for reverse proxy mode."""

import json
import logging
import os
import queue
import secrets
import threading
```

---

### Task 2: Add `is_sse` field to `StreamingState`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:43-63`

`StreamingState` is at line 43. Its fields with defaults end at lines 60–62:

```python
    headers_sent: bool = False  # guards error-response logic       ← line 60
    status_code: int = 0  # stored after _ResponseHeaders item is processed  ← line 61
    error: BaseException | None = None  # set by worker on exception ← line 62
```

- [ ] **Step 1: Add `is_sse` field after `headers_sent` (after line 60)**

```python
    headers_sent: bool = False  # guards error-response logic
    is_sse: bool = False  # set by read_from_descriptors when _ResponseHeaders processed
    status_code: int = 0  # stored after _ResponseHeaders item is processed
    error: BaseException | None = None  # set by worker on exception
```

---

### Task 3: Write the failing tests (TDD)

**Files:**
- Modify: `tests/test_web_server_plugin.py`

All new tests go inside `class TestEventLoopHooks` (line 521), which contains `_make_state_with_pipe`. Add the four new tests after the last existing test in that class (`test_worker_error_after_headers_does_not_send_error_response`, around line 743).

- [ ] **Step 1: Write test — `is_sse` propagation from `_ResponseHeaders`**

```python
def test_is_sse_propagated_to_state_when_response_headers_processed(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """read_from_descriptors sets state.is_sse=True from _ResponseHeaders(is_sse=True)."""
    import asyncio
    import os

    state, pipe_r, pipe_w = self._make_state_with_pipe()
    assert state.is_sse is False  # default
    state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, True))  # is_sse=True
    plugin._streaming_state = state
    os.write(pipe_w, b"\x00")
    try:
        asyncio.run(plugin.read_from_descriptors([pipe_r]))
    finally:
        plugin._streaming_state = None
        for fd in (pipe_r, pipe_w):
            try:
                os.close(fd)
            except OSError:
                pass
    assert state.is_sse is True
```

- [ ] **Step 2: Write test — SSE error event injected on mid-stream error**

```python
def test_sse_error_event_sent_when_error_after_headers_on_sse_stream(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """_finish_stream injects SSE error event when error occurs after headers on SSE stream."""
    import asyncio
    import json
    import os

    state, pipe_r, pipe_w = self._make_state_with_pipe()
    state.error = RuntimeError("upstream boom")
    state.headers_sent = True
    state.is_sse = True
    state.chunk_queue.put(None)
    plugin._streaming_state = state
    os.write(pipe_w, b"\x00")
    plugin.client.queue.reset_mock()  # type: ignore[attr-defined]
    asyncio.run(plugin.read_from_descriptors([pipe_r]))

    # Must have queued exactly one item — the SSE error event
    assert plugin.client.queue.call_count == 1  # type: ignore[attr-defined]
    queued = bytes(plugin.client.queue.call_args[0][0])  # type: ignore[attr-defined]
    expected_payload = json.dumps({
        "type": "error",
        "error": {"type": "api_error", "message": "Upstream connection lost"},
    })
    expected = f"event: error\ndata: {expected_payload}\n\n".encode()
    assert queued == expected
```

- [ ] **Step 3: Write test — non-SSE silent close preserved**

```python
def test_non_sse_error_after_headers_still_silent(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """_finish_stream does NOT send anything when error occurs after headers on non-SSE stream."""
    import asyncio
    import os

    state, pipe_r, pipe_w = self._make_state_with_pipe()
    state.error = RuntimeError("upstream boom")
    state.headers_sent = True
    state.is_sse = False  # explicit — non-SSE path
    state.chunk_queue.put(None)
    plugin._streaming_state = state
    os.write(pipe_w, b"\x00")
    plugin.client.queue.reset_mock()  # type: ignore[attr-defined]
    asyncio.run(plugin.read_from_descriptors([pipe_r]))

    assert not plugin.client.queue.called  # type: ignore[attr-defined]
    plugin.logger.warning.assert_called()  # type: ignore[attr-defined]
```

- [ ] **Step 4: Write test — error before headers still sends 503 (regression guard)**

This complements the existing `test_worker_error_before_headers_sends_503` (line 725) by also asserting `b"503"` appears in the queued bytes:

```python
def test_error_before_headers_sends_503_bytes(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """Regression guard: _finish_stream sends a 503 response when error precedes headers."""
    import asyncio
    import os

    state, pipe_r, pipe_w = self._make_state_with_pipe()
    state.error = RuntimeError("pre-header boom")
    state.headers_sent = False
    state.is_sse = False
    state.chunk_queue.put(None)
    plugin._streaming_state = state
    os.write(pipe_w, b"\x00")
    asyncio.run(plugin.read_from_descriptors([pipe_r]))

    assert plugin.client.queue.called  # type: ignore[attr-defined]
    queued = bytes(plugin.client.queue.call_args_list[0][0][0])  # type: ignore[attr-defined]
    assert b"503" in queued
```

- [ ] **Step 5: Update existing test to set `is_sse = False` explicitly**

Find `test_worker_error_after_headers_does_not_send_error_response` (line 743). It already sets `state.headers_sent = True`. Add `state.is_sse = False` immediately after:

```python
        state.error = RuntimeError("boom after headers")
        state.headers_sent = True
        state.is_sse = False  # explicit: tests non-SSE silent-close path
```

- [ ] **Step 6: Run the four new tests — verify expected outcomes**

```bash
pytest tests/test_web_server_plugin.py::TestEventLoopHooks::test_is_sse_propagated_to_state_when_response_headers_processed \
       tests/test_web_server_plugin.py::TestEventLoopHooks::test_sse_error_event_sent_when_error_after_headers_on_sse_stream \
       tests/test_web_server_plugin.py::TestEventLoopHooks::test_non_sse_error_after_headers_still_silent \
       tests/test_web_server_plugin.py::TestEventLoopHooks::test_error_before_headers_sends_503_bytes \
       -v
```

Expected:
- `test_is_sse_propagated_to_state_when_response_headers_processed` — **FAIL** (`is_sse` field not yet added)
- `test_sse_error_event_sent_when_error_after_headers_on_sse_stream` — **FAIL** (`is_sse` field / `_send_sse_error_event` not yet added)
- `test_non_sse_error_after_headers_still_silent` — **PASS** (guards existing non-SSE silent-close behaviour)
- `test_error_before_headers_sends_503_bytes` — **PASS** (guards existing 503 behaviour)

---

### Task 4: Implement `read_from_descriptors` — propagate `is_sse`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:155-158`

The `_ResponseHeaders` block in `read_from_descriptors` is at lines 155–158:

```python
            if isinstance(item, _ResponseHeaders):
                state.status_code = item.status_code
                self._send_response_headers_from(item)
                state.headers_sent = True
```

- [ ] **Step 1: Add `state.is_sse = item.is_sse` as the first line inside the block**

```python
            if isinstance(item, _ResponseHeaders):
                state.is_sse = item.is_sse
                state.status_code = item.status_code
                self._send_response_headers_from(item)
                state.headers_sent = True
```

- [ ] **Step 2: Run propagation test — verify it now passes**

```bash
pytest tests/test_web_server_plugin.py::TestEventLoopHooks::test_is_sse_propagated_to_state_when_response_headers_processed -v
```

Expected: PASS.

---

### Task 5: Implement `_send_sse_error_event` and update `_finish_stream`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

**Sub-task A — Add `_send_sse_error_event` method**

Add the new method after `_send_error`. `_send_error` ends at line 532; add immediately after:

- [ ] **Step 1: Add `_send_sse_error_event` after `_send_error`**

```python
    def _send_sse_error_event(self) -> None:
        """Inject a synthetic SSE error event to notify the client of upstream failure.

        Called from _finish_stream (main thread only).
        Thread-safety: self.client.queue() is only called from the main thread.
        """
        payload = json.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": "Upstream connection lost"},
        })
        event = f"event: error\ndata: {payload}\n\n".encode()
        self.client.queue(memoryview(event))
```

**Sub-task B — Update `_finish_stream`**

`_finish_stream` error branch is at lines 179–183:

```python
        if state.error:
            if not state.headers_sent:
                self._send_error(503, "Upstream error")
            log_func = self.logger.warning if state.headers_sent else self.logger.error
            log_func("Stream ended with error: %s", state.error)
```

- [ ] **Step 2: Insert `elif state.is_sse` branch between the 503 path and the log lines**

```python
        if state.error:
            if not state.headers_sent:
                self._send_error(503, "Upstream error")
            elif state.is_sse:
                self._send_sse_error_event()
            # non-SSE + headers sent: silent close (unchanged)
            log_func = self.logger.warning if state.headers_sent else self.logger.error
            log_func("Stream ended with error: %s", state.error)
```

- [ ] **Step 3: Run all four new tests — verify they all pass**

```bash
pytest tests/test_web_server_plugin.py::TestEventLoopHooks::test_is_sse_propagated_to_state_when_response_headers_processed \
       tests/test_web_server_plugin.py::TestEventLoopHooks::test_sse_error_event_sent_when_error_after_headers_on_sse_stream \
       tests/test_web_server_plugin.py::TestEventLoopHooks::test_non_sse_error_after_headers_still_silent \
       tests/test_web_server_plugin.py::TestEventLoopHooks::test_error_before_headers_sends_503_bytes \
       -v
```

Expected: all 4 PASS.

- [ ] **Step 4: Run the full test suite — verify no regressions**

```bash
pytest tests/ -v
```

Expected: all tests PASS.

---

### Task 6: Commit

**Files:**
- `flow_proxy_plugin/plugins/web_server_plugin.py`
- `tests/test_web_server_plugin.py`

- [ ] **Step 1: Run linters**

```bash
make lint
```

Expected: no errors.

- [ ] **Step 2: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: inject SSE error event on mid-stream backend failure"
```
