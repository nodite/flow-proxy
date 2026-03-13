# Async Streaming (B3) Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `FlowProxyWebServerPlugin` so that response chunks are delivered to the client immediately as they arrive from the backend, instead of accumulating until the full response is buffered.

**Architecture:** `handle_request()` returns immediately after starting a worker thread; the worker feeds chunks through a `queue.Queue` and notifies the event loop via `os.pipe()`; proxy.py's native `handle_writables()` calls `flush()` on each event loop iteration, delivering data to the client in real time. Thread-safety invariant: `self.client` is only ever touched by the main (event loop) thread.

**Tech Stack:** Python 3.12+, `queue.Queue`, `os.pipe()`, `threading.Thread`, proxy.py `get_descriptors()` / `read_from_descriptors()` hooks, httpx streaming

**Spec:** `docs/superpowers/specs/2026-03-13-async-streaming-design.md`

---

## Chunk 1: Data Structures and Helper Methods

### Task 1: Add `StreamingState` and `_ResponseHeaders` dataclasses

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`
- Test: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing tests for the two new dataclasses**

Add at the top of `tests/test_web_server_plugin.py`, in a new `TestDataStructures` class:

```python
import queue
from flow_proxy_plugin.plugins.web_server_plugin import (
    StreamingState,
    _ResponseHeaders,
)

class TestDataStructures:
    def test_streaming_state_defaults(self) -> None:
        import os, threading
        pipe_r, pipe_w = os.pipe()
        try:
            state = StreamingState(
                pipe_r=pipe_r,
                pipe_w=pipe_w,
                chunk_queue=queue.Queue(),
                thread=None,
                cancel=threading.Event(),
                req_id="abc123",
                config_name="test-config",
            )
            assert state.headers_sent is False
            assert state.status_code == 0
            assert state.error is None
        finally:
            os.close(pipe_r)
            os.close(pipe_w)

    def test_response_headers_fields(self) -> None:
        h = _ResponseHeaders(
            status_code=200,
            reason_phrase="OK",
            headers={"content-type": "text/event-stream"},
            is_sse=True,
        )
        assert h.is_sse is True
        assert h.headers["content-type"] == "text/event-stream"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/kang/Projects/flow-proxy
pytest tests/test_web_server_plugin.py::TestDataStructures -v
```

Expected: `ImportError` — `StreamingState` and `_ResponseHeaders` not yet defined.

- [ ] **Step 3: Add the dataclasses to `web_server_plugin.py`**

Add these two dataclasses after the existing `StreamStats` dataclass (after line 66), and add `import queue` to the imports at the top:

```python
@dataclass
class _ResponseHeaders:
    """Response metadata passed from worker thread to main thread.

    Contains only plain Python types — no httpx objects cross thread boundaries.
    """
    status_code: int
    reason_phrase: str
    headers: dict[str, str]  # extracted from httpx.Headers
    is_sse: bool              # True if Content-Type: text/event-stream


@dataclass
class StreamingState:
    """Per-request streaming state for the B3 async pipe-mediated pattern.

    Stored as self._streaming_state. Initialized to None in __init__ and _rebind().
    Thread-safety: self.client is ONLY touched by the main thread.
    state.error is written by worker before queue.put(None) and read by main
    thread after queue.get() returns None — GIL guarantees visibility in CPython.
    """
    pipe_r: int                          # registered with selector as readable fd
    pipe_w: int                          # worker writes notification bytes here
    chunk_queue: "queue.Queue[_ResponseHeaders | bytes | None]"
    thread: threading.Thread | None      # None until assigned in handle_request()
    cancel: threading.Event              # set by _reset_request_state() on disconnect
    req_id: str                          # for log context
    config_name: str                     # for final access log line
    headers_sent: bool = False           # guards error-response logic
    status_code: int = 0                 # stored after _ResponseHeaders item is processed
    error: BaseException | None = None   # set by worker on exception
```

Also add `import queue` to the imports block at the top of the file.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_web_server_plugin.py::TestDataStructures -v
```

Expected: 2 passed.

- [ ] **Step 5: Run full linting**

```bash
make lint
```

Expected: all checks pass.

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add StreamingState and _ResponseHeaders dataclasses for async streaming"
```

---

### Task 2: Add `_encode_sse_line()` and `_send_response_headers_from()`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`
- Test: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing tests**

Add to `TestDataStructures` (or a new class `TestHelperMethods`):

```python
class TestHelperMethods:
    def test_encode_sse_line_empty_string_gives_newline(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        result = plugin._encode_sse_line("")
        assert result == b"\n"

    def test_encode_sse_line_content_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        result = plugin._encode_sse_line("data: hello")
        assert result == b"data: hello\n"

    def test_send_response_headers_from_status_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"content-type": "application/json"}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]
        assert b"HTTP/1.1 200 OK\r\n" in calls

    def test_send_response_headers_from_strips_connection(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"connection": "keep-alive", "content-type": "text/plain"}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]
        assert not any(b"connection" in c.lower() for c in calls)

    def test_send_response_headers_from_strips_transfer_encoding_for_non_sse(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"transfer-encoding": "chunked"}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]
        assert not any(b"transfer-encoding" in c.lower() for c in calls)

    def test_send_response_headers_from_keeps_transfer_encoding_for_sse(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"transfer-encoding": "chunked", "content-type": "text/event-stream"}, True)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]
        assert any(b"transfer-encoding" in c.lower() for c in calls)

    def test_send_response_headers_from_adds_sse_anti_buffer_headers(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {"content-type": "text/event-stream"}, True)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]
        assert b"Cache-Control: no-cache\r\n" in calls
        assert b"X-Accel-Buffering: no\r\n" in calls

    def test_send_response_headers_from_ends_with_blank_line(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        h = _ResponseHeaders(200, "OK", {}, False)
        plugin._send_response_headers_from(h)
        calls = [bytes(c.args[0]) for c in plugin.client.queue.call_args_list]
        assert calls[-1] == b"\r\n"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_web_server_plugin.py::TestHelperMethods -v
```

Expected: `AttributeError` — methods not yet defined.

- [ ] **Step 3: Add the helper methods to `FlowProxyWebServerPlugin`**

Add these methods after `_send_response_headers()` (around line 360):

```python
@staticmethod
def _encode_sse_line(raw: str) -> bytes:
    """Encode a single SSE line to bytes.

    Empty string (event boundary separator) → b'\\n'.
    Content line → (raw + '\\n').encode().
    """
    if raw == "":
        return b"\n"
    return (raw + "\n").encode()

def _send_response_headers_from(self, item: _ResponseHeaders) -> None:
    """Send HTTP status line and headers from a _ResponseHeaders item.

    Called from the main thread only (read_from_descriptors path).
    Mirrors _send_response_headers() but accepts plain-data _ResponseHeaders
    instead of an httpx.Response.
    """
    status_line = f"HTTP/1.1 {item.status_code} {item.reason_phrase}\r\n"
    self.client.queue(memoryview(status_line.encode()))

    skip_headers = {"connection"}
    if not item.is_sse:
        skip_headers.add("transfer-encoding")

    for name, value in item.headers.items():
        if name.lower() not in skip_headers:
            self.client.queue(memoryview(f"{name}: {value}\r\n".encode()))

    if item.is_sse:
        self.client.queue(memoryview(b"Cache-Control: no-cache\r\n"))
        self.client.queue(memoryview(b"X-Accel-Buffering: no\r\n"))

    self.client.queue(memoryview(b"\r\n"))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_web_server_plugin.py::TestHelperMethods -v
```

Expected: all passed.

- [ ] **Step 5: Run full linting**

```bash
make lint
```

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add _encode_sse_line and _send_response_headers_from helpers"
```

---

## Chunk 2: Worker Thread and Event Loop Hooks

### Task 3: Add `_streaming_worker()`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`
- Test: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing tests**

```python
class TestStreamingWorker:
    """Tests for _streaming_worker() background thread method."""

    def _make_state(self) -> "StreamingState":
        import os, threading, queue as q
        pipe_r, pipe_w = os.pipe()
        return StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=q.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="test01",
            config_name="cfg",
        )

    def test_worker_puts_headers_then_chunks_then_sentinel(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """Worker puts _ResponseHeaders, byte chunks, then None sentinel in order."""
        import os
        state = self._make_state()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.iter_bytes.return_value = iter([b"chunk1", b"chunk2"])
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        items = []
        while not state.chunk_queue.empty():
            items.append(state.chunk_queue.get_nowait())

        assert isinstance(items[0], _ResponseHeaders)
        assert items[1] == b"chunk1"
        assert items[2] == b"chunk2"
        assert items[-1] is None  # sentinel
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_sse_encodes_lines(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """SSE lines are encoded: empty string → b'\\n', content → bytes with \\n."""
        import os
        state = self._make_state()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"content-type": "text/event-stream"})
        mock_response.iter_lines.return_value = iter(["data: hi", "", "data: bye"])
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("POST", "https://example.com", {}, None, state)

        items = []
        while not state.chunk_queue.empty():
            items.append(state.chunk_queue.get_nowait())

        assert isinstance(items[0], _ResponseHeaders)  # headers
        assert items[1] == b"data: hi\n"
        assert items[2] == b"\n"          # SSE event boundary
        assert items[3] == b"data: bye\n"
        assert items[-1] is None
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_cancel_stops_iteration(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """cancel_event causes worker to break out of the iteration loop."""
        import os, threading
        state = self._make_state()
        state.cancel.set()  # pre-set cancel before worker starts

        def slow_iter():
            yield b"first"
            yield b"should not appear"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.reason_phrase = "OK"
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.iter_bytes.return_value = slow_iter()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_svc.http_client.stream.return_value = mock_response

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        items = []
        while not state.chunk_queue.empty():
            items.append(state.chunk_queue.get_nowait())

        # First chunk may or may not be present (cancel checked before enqueue),
        # but sentinel must be last and "should not appear" must not be present.
        assert items[-1] is None
        byte_items = [i for i in items if isinstance(i, bytes)]
        assert b"should not appear" not in byte_items
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_transport_error_sets_state_error_and_sentinel(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """On httpx.TransportError, worker stores error, marks client dirty, puts sentinel."""
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = httpx.TransportError("conn failed")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, httpx.TransportError)
        assert state.chunk_queue.get_nowait() is None  # sentinel
        mock_svc.mark_http_client_dirty.assert_called_once()
        os.close(state.pipe_r)
        os.close(state.pipe_w)

    def test_worker_generic_exception_sets_error_and_sentinel(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        import os
        state = self._make_state()
        mock_svc.http_client.stream.side_effect = RuntimeError("boom")

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin._streaming_worker("GET", "https://example.com", {}, None, state)

        assert isinstance(state.error, RuntimeError)
        assert state.chunk_queue.get_nowait() is None
        os.close(state.pipe_r)
        os.close(state.pipe_w)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_web_server_plugin.py::TestStreamingWorker -v
```

Expected: `AttributeError` — `_streaming_worker` not yet defined.

- [ ] **Step 3: Add `_streaming_worker()` to `FlowProxyWebServerPlugin`**

Add after `_send_response_headers_from()`:

```python
def _streaming_worker(
    self,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    state: StreamingState,
) -> None:
    """Background thread: opens httpx stream, feeds chunks into state.chunk_queue.

    Queue protocol (strict order):
      1. _ResponseHeaders  — response metadata for main thread to send headers
      2. bytes             — one item per non-empty chunk/encoded SSE line
      3. None              — sentinel, always last (even on error)

    Never touches self.client. Thread-safety invariant: only main thread uses self.client.
    """
    set_request_context(state.req_id, "WS")
    try:
        http_client = ProcessServices.get().get_http_client()
        with http_client.stream(
            method=method,
            url=url,
            headers=headers,
            content=body,
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
        ) as response:
            is_sse = "text/event-stream" in response.headers.get("content-type", "")
            self.logger.info(
                "Backend response: %d %s, Transfer-Encoding: %s, Content-Length: %s",
                response.status_code,
                response.reason_phrase,
                response.headers.get("transfer-encoding", "none"),
                response.headers.get("content-length", "none"),
            )
            state.chunk_queue.put(
                _ResponseHeaders(
                    status_code=response.status_code,
                    reason_phrase=response.reason_phrase,
                    headers=dict(response.headers),
                    is_sse=is_sse,
                )
            )
            try:
                os.write(state.pipe_w, b"\x00")
            except OSError:
                return  # client already disconnected

            first_logged = False
            if is_sse:
                for line in response.iter_lines():
                    if state.cancel.is_set():
                        break
                    chunk = self._encode_sse_line(line)
                    if chunk:
                        if not first_logged:
                            self.logger.info(
                                "Received first SSE line from backend: %d chars", len(line)
                            )
                            first_logged = True
                        state.chunk_queue.put(chunk)
                        try:
                            os.write(state.pipe_w, b"\x00")
                        except OSError:
                            return
            else:
                for chunk in response.iter_bytes():
                    if state.cancel.is_set():
                        break
                    if not chunk:
                        continue
                    if not first_logged:
                        self.logger.info(
                            "Received first chunk from backend: %d bytes", len(chunk)
                        )
                        first_logged = True
                    state.chunk_queue.put(chunk)
                    try:
                        os.write(state.pipe_w, b"\x00")
                    except OSError:
                        return

    except httpx.TransportError as e:
        self.logger.error("Transport error — marking httpx client dirty: %s", e)
        ProcessServices.get().mark_http_client_dirty()
        state.error = e
    except Exception as e:
        self.logger.error("Worker error: %s", e, exc_info=True)
        state.error = e
    finally:
        state.chunk_queue.put(None)
        try:
            os.write(state.pipe_w, b"\x00")
        except OSError:
            pass  # pipe may already be closed (client disconnected)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_web_server_plugin.py::TestStreamingWorker -v
```

Expected: all passed.

- [ ] **Step 5: Run full linting**

```bash
make lint
```

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add _streaming_worker background thread for async streaming"
```

---

### Task 4: Add event loop hooks and state lifecycle methods

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`
- Test: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing tests**

```python
class TestEventLoopHooks:
    """Tests for get_descriptors, read_from_descriptors, _finish_stream, _reset_request_state."""

    def _make_state_with_pipe(self) -> tuple["StreamingState", int, int]:
        import os, threading, queue as q
        pipe_r, pipe_w = os.pipe()
        state = StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=q.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="abc",
            config_name="cfg",
        )
        return state, pipe_r, pipe_w

    def test_get_descriptors_empty_when_no_state(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio
        plugin._streaming_state = None
        r, w = asyncio.run(plugin.get_descriptors())
        assert r == []
        assert w == []

    def test_get_descriptors_returns_pipe_r_when_streaming(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        plugin._streaming_state = state
        try:
            r, w = asyncio.run(plugin.get_descriptors())
            assert r == [pipe_r]
            assert w == []
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_noop_when_pipe_not_in_readables(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        plugin._streaming_state = state
        try:
            result = asyncio.run(
                plugin.read_from_descriptors([999])  # wrong fd
            )
            assert result is False
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_queues_each_chunk(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, False))
        state.chunk_queue.put(b"hello")
        state.chunk_queue.put(b"world")
        plugin._streaming_state = state
        # Write notification bytes so os.read doesn't block
        os.write(pipe_w, b"\x00\x00\x00")
        try:
            result = asyncio.run(
                plugin.read_from_descriptors([pipe_r])
            )
            assert result is False
            # _send_response_headers_from({}, not SSE) queues: status line + blank line = 2 calls
            # Plus 2 byte chunks = 4 total
            assert plugin.client.queue.call_count == 4
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_sends_headers_on_first_item(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_send_response_headers_from is called exactly once, before any byte chunks."""
        import asyncio, os
        from unittest.mock import patch, call
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, False))
        state.chunk_queue.put(b"first-chunk")
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00\x00")
        try:
            with patch.object(plugin, "_send_response_headers_from") as mock_send_headers:
                asyncio.run(plugin.read_from_descriptors([pipe_r]))
            # Headers sent exactly once
            assert mock_send_headers.call_count == 1
            # Headers sent before the byte chunk (queue called once for the chunk)
            assert plugin.client.queue.call_count == 1
            assert plugin.client.queue.call_args == call(memoryview(b"first-chunk"))
        finally:
            plugin._streaming_state = None
            os.close(pipe_r)
            os.close(pipe_w)

    def test_read_from_descriptors_returns_true_on_sentinel(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.status_code = 200
        state.config_name = "cfg"
        state.chunk_queue.put(None)  # sentinel only
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        result = asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        assert result is True
        assert plugin._streaming_state is None  # _finish_stream cleared it

    def test_get_descriptors_empty_after_stream_finishes(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """After sentinel is processed, get_descriptors() returns []."""
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.status_code = 200
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        r, w = asyncio.run(plugin.get_descriptors())
        assert r == []

    def test_reset_request_state_sets_cancel_and_closes_fds(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import os, threading
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        state.thread = t
        plugin._streaming_state = state
        plugin._reset_request_state()
        assert state.cancel.is_set()
        assert plugin._streaming_state is None
        # fds should be closed — attempting to close them again should raise OSError
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)

    def test_reset_is_idempotent_when_state_is_none(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        plugin._streaming_state = None
        plugin._reset_request_state()  # should not raise

    def test_reset_is_safe_when_join_times_out(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """_reset_request_state closes fds even when join() times out (blocked worker)."""
        import os, threading
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        # Thread that blocks until cancel is set — simulates unresponsive upstream
        block = threading.Event()
        t = threading.Thread(target=lambda: block.wait(timeout=60), daemon=True)
        t.start()
        state.thread = t
        plugin._streaming_state = state
        # join(timeout=2.0) would block for 2s; patch it to return immediately
        from unittest.mock import patch
        with patch.object(t, "join", return_value=None):
            plugin._reset_request_state()
        # fds must be closed regardless of join timeout
        with pytest.raises(OSError):
            os.close(pipe_r)
        with pytest.raises(OSError):
            os.close(pipe_w)
        assert plugin._streaming_state is None
        block.set()  # let daemon thread exit cleanly

    def test_worker_error_before_headers_sends_503(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("boom")
        state.headers_sent = False
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        # _send_error should have been called — client.queue should contain error response
        assert plugin.client.queue.called

    def test_worker_error_after_headers_does_not_send_error_response(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        import asyncio, os
        state, pipe_r, pipe_w = self._make_state_with_pipe()
        state.error = RuntimeError("boom after headers")
        state.headers_sent = True
        state.chunk_queue.put(None)
        plugin._streaming_state = state
        os.write(pipe_w, b"\x00")
        plugin.client.queue.reset_mock()
        asyncio.run(
            plugin.read_from_descriptors([pipe_r])
        )
        # No error response queued when headers already sent
        assert not plugin.client.queue.called
        # Error must be logged (warning-level when headers already committed)
        plugin.logger.warning.assert_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_web_server_plugin.py::TestEventLoopHooks -v
```

Expected: `AttributeError` — hooks not yet defined.

- [ ] **Step 3: Add `_streaming_state` init, `_finish_stream`, `_reset_request_state`, `get_descriptors`, `read_from_descriptors` to the plugin**

**3a.** In `__init__` (after `self._init_services()`), add:
```python
self._streaming_state: StreamingState | None = None
```

**3b.** In `_rebind()` (after `self._pooled = True`), add:
```python
self._streaming_state = None
```

**3c.** Add these methods after `on_client_connection_close()`:

```python
async def get_descriptors(self) -> tuple[list[int], list[int]]:
    """Register pipe_r with proxy.py's selector while streaming is active."""
    if self._streaming_state is not None:
        return [self._streaming_state.pipe_r], []
    return [], []

async def read_from_descriptors(self, r: list[int]) -> bool:
    """Drain chunk_queue when pipe_r is readable; queue chunks to self.client.

    Returns True (teardown signal) when the sentinel is processed.
    Invariant: only this method (main thread) calls self.client.queue().
    """
    state = self._streaming_state
    if state is None or state.pipe_r not in r:
        return False

    # Drain notification bytes (batch). More bytes than 256 cause harmless
    # re-entry on the next select cycle with an empty queue — self-correcting.
    os.read(state.pipe_r, 256)
    set_request_context(state.req_id, "WS")

    while not state.chunk_queue.empty():
        item = state.chunk_queue.get_nowait()

        if isinstance(item, _ResponseHeaders):
            state.status_code = item.status_code
            self._send_response_headers_from(item)
            state.headers_sent = True

        elif item is None:  # sentinel — stream ended or errored
            self._finish_stream(state)
            return True     # signal proxy.py to close connection

        else:  # bytes chunk
            self.client.queue(memoryview(item))

    return False

def _finish_stream(self, state: StreamingState) -> None:
    """Close pipe fds, clear state, log completion. Called from main thread only."""
    # Clear first so get_descriptors() immediately returns [] on next call
    self._streaming_state = None
    for fd in (state.pipe_r, state.pipe_w):
        try:
            os.close(fd)
        except OSError:
            pass

    if state.error:
        if not state.headers_sent:
            self._send_error(503, "Upstream error")
        log_func = self.logger.warning if state.headers_sent else self.logger.error
        log_func("Stream ended with error: %s", state.error)
    else:
        log_func = (
            self.logger.info if state.status_code < 400 else self.logger.warning
        )
        log_func("← %d [%s]", state.status_code, state.config_name)
    clear_request_context()

def _reset_request_state(self) -> None:
    """Cancel and join worker thread; close pipe fds. Called by PluginPool.release()."""
    state = self._streaming_state
    if state is None:
        return
    state.cancel.set()
    if state.thread is not None:
        state.thread.join(timeout=2.0)
        # If join times out, thread is abandoned as daemon; it holds no reference
        # to self.client so no further socket writes occur.
    for fd in (state.pipe_r, state.pipe_w):
        try:
            os.close(fd)
        except OSError:
            pass
    self._streaming_state = None
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_web_server_plugin.py::TestEventLoopHooks -v
```

Expected: all passed.

- [ ] **Step 5: Run full linting**

```bash
make lint
```

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add event loop hooks and streaming state lifecycle methods"
```

---

## Chunk 3: Refactor `handle_request()` and Clean Up

### Task 5: Refactor `handle_request()` to use the async pattern

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`
- Test: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing tests for the new `handle_request()` behaviour**

```python
class TestHandleRequestAsync:
    """Tests for the refactored handle_request() that starts a worker and returns."""

    def test_handle_request_starts_worker_thread(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """handle_request() returns immediately and sets _streaming_state."""
        mock_svc.load_balancer.get_next_config.return_value = {
            "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
        }
        mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
        mock_svc.request_filter.find_matching_rule.return_value = None
        mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

        # Make http_client.stream block until we release it
        import threading
        ready = threading.Event()
        released = threading.Event()

        def blocking_stream(*a, **kw):
            ready.set()
            released.wait(timeout=2)
            return MagicMock(__enter__=MagicMock(return_value=MagicMock(
                status_code=200, reason_phrase="OK",
                headers=httpx.Headers({"content-type": "application/json"}),
                iter_bytes=MagicMock(return_value=iter([])),
            )), __exit__=MagicMock(return_value=False))

        mock_svc.http_client.stream.side_effect = blocking_stream

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin.handle_request(request)

        # handle_request() returned — state should be set
        assert plugin._streaming_state is not None
        released.set()  # let the worker finish

    def test_handle_request_cleans_up_pipe_on_auth_failure(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """If auth fails, handle_request sends error and returns without leaking fds."""
        mock_svc.load_balancer.get_next_config.side_effect = Exception("no config")

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        with patch.object(ProcessServices, "get", return_value=mock_svc):
            plugin.handle_request(request)

        assert plugin._streaming_state is None
        # _send_error should have been called
        assert plugin.client.queue.called

    def test_handle_request_cleans_up_pipe_on_setup_failure(
        self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
    ) -> None:
        """If threading.Thread.start() raises, pipe fds are closed and state is None."""
        mock_svc.load_balancer.get_next_config.return_value = {
            "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
        }
        mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
        mock_svc.request_filter.find_matching_rule.return_value = None
        mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/test"
        request.headers = {}
        request.body = None
        request.buffer = None

        import os
        captured_fds: list[int] = []
        real_pipe = os.pipe

        def capturing_pipe() -> tuple[int, int]:
            r, w = real_pipe()
            captured_fds.extend([r, w])
            return r, w

        with patch("flow_proxy_plugin.plugins.web_server_plugin.os.pipe", side_effect=capturing_pipe):
            with patch("threading.Thread.start", side_effect=RuntimeError("thread start failed")):
                with patch.object(ProcessServices, "get", return_value=mock_svc):
                    plugin.handle_request(request)

        assert plugin._streaming_state is None
        # _send_error should have been called for the setup failure
        assert plugin.client.queue.called
        # Both pipe fds must have been closed — re-closing should raise OSError
        assert len(captured_fds) == 2
        with pytest.raises(OSError):
            os.close(captured_fds[0])
        with pytest.raises(OSError):
            os.close(captured_fds[1])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_web_server_plugin.py::TestHandleRequestAsync -v
```

Expected: failures because `handle_request()` still uses the old synchronous pattern.

- [ ] **Step 3: Replace `handle_request()` with the async version**

Replace the entire `handle_request()` method body (lines 136–217 in current file):

```python
def handle_request(self, request: HttpParser) -> None:
    """Start async streaming: authenticate, build params, launch worker, return.

    Does NOT block waiting for the upstream response. The worker thread feeds
    chunks through StreamingState; read_from_descriptors() delivers them to
    the client via proxy.py's event loop.
    """
    method = self._decode_bytes(request.method) if request.method else "GET"
    path = self._decode_bytes(request.path) if request.path else "/"

    req_id = secrets.token_hex(3)
    set_request_context(req_id, "WS")
    self.logger.info("→ %s %s", method, path)

    try:
        _, config_name, jwt_token = self._get_config_and_token()
    except Exception as e:
        self.logger.error("Auth failed: %s", e)
        self._send_error(503, "Auth error")
        clear_request_context()
        return

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

    pipe_r, pipe_w = os.pipe()
    try:
        state = StreamingState(
            pipe_r=pipe_r,
            pipe_w=pipe_w,
            chunk_queue=queue.Queue(),
            cancel=threading.Event(),
            req_id=req_id,
            config_name=config_name,
            thread=None,  # type: ignore[arg-type]
        )
        state.thread = threading.Thread(
            target=self._streaming_worker,
            args=(method, target_url, headers, body, state),
            name=f"streaming-{req_id}",
            daemon=True,
        )
        self._streaming_state = state
        state.thread.start()
    except Exception:
        try:
            os.close(pipe_r)
        except OSError:
            pass
        try:
            os.close(pipe_w)
        except OSError:
            pass
        self._streaming_state = None
        self._send_error(500, "Failed to start streaming")
        clear_request_context()
    # Return immediately — event loop resumes, read_from_descriptors delivers data
```

Note: `clear_request_context()` is **not** called at the end of the normal path — it is deferred to `_finish_stream()` once the stream ends.

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_web_server_plugin.py::TestHandleRequestAsync -v
```

Expected: all passed.

- [ ] **Step 5: Run full test suite**

```bash
make test
```

Expected: all passed (some old tests that tested the blocking path may need updating — see Task 6).

- [ ] **Step 6: Run full linting**

```bash
make lint
```

- [ ] **Step 7: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: refactor handle_request to async pipe-mediated streaming pattern"
```

---

### Task 6: Update and remove obsolete tests

**Files:**
- Modify: `tests/test_web_server_plugin.py`

After the `handle_request()` refactor, any tests that relied on `handle_request()` synchronously completing the full stream (e.g., mocking `http_client.stream` and asserting `client.queue` calls inline) will be broken. These tests were valid for the old synchronous design but test the wrong thing now.

- [ ] **Step 1: Identify failing tests**

```bash
pytest tests/test_web_server_plugin.py -v 2>&1 | grep FAILED
```

- [ ] **Step 2: Remove the following test classes entirely**

These classes test methods that no longer exist after the refactor:

- `TestStreamBytes` — tests `_stream_bytes()`, which is removed
- `TestStreamSSE` — tests `_stream_sse()`, which is removed
- `TestStreamResponseBody` — tests `_stream_response_body()`, which is removed
- `TestSendResponseHeaders` — tests `_send_response_headers(httpx.Response)`, which is removed
- `TestLogStreamStats` — tests `_log_stream_stats()`, which is removed

Also remove `TestStreamStats` — the `StreamStats` dataclass is removed in Task 7.

- [ ] **Step 3: Remove or rewrite tests in `TestHandleRequest`**

`TestHandleRequest` calls `plugin.handle_request(request)` and then immediately asserts `plugin.client.queue.call_args_list`. Under the async design, `handle_request()` returns before any data is queued (data arrives via `read_from_descriptors()`). These tests must be removed or rewritten.

**Remove entirely** (behaviour now covered by `TestHandleRequestAsync` + `TestStreamingWorker`):
- `test_handle_request_success_queues_response` — asserts queued bytes synchronously; invalid under async
- `test_handle_request_failure_sends_error` — asserts `_send_error` synchronously; invalid under async
- `test_handle_request_with_buffer_body` — asserts queued bytes synchronously; invalid under async
- `test_handle_request_remote_protocol_error_does_not_send_error` — passes `httpx.RemoteProtocolError` to `stream()` which now runs in worker thread; invalid under async

**Rewrite** `test_handle_request_timeout_configuration` — the current test asserts `captured_timeout` after `handle_request()` returns, but under the async design the worker thread may not have called `stream()` yet (race condition). Rewrite it to synchronize on the stream call:

```python
def test_handle_request_timeout_configuration(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
) -> None:
    """Worker passes httpx.Timeout(connect=30s, read=600s) to stream()."""
    import threading
    captured_timeout: list[httpx.Timeout] = []
    stream_called = threading.Event()

    def capture_stream(**kwargs: Any) -> MagicMock:
        captured_timeout.append(kwargs.get("timeout"))
        stream_called.set()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.reason_phrase = "OK"
        mock_resp.headers = httpx.Headers({"content-type": "application/json"})
        mock_resp.iter_bytes.return_value = iter([])
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    mock_svc.http_client.stream.side_effect = capture_stream

    request = Mock(spec=HttpParser)
    request.method = b"POST"
    request.path = b"/v1/chat/completions"
    request.body = b"{}"
    request.headers = {}

    mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
    with (
        patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
        patch.object(ProcessServices, "get", return_value=mock_svc),
    ):
        plugin.handle_request(request)

    # Wait for worker thread to call stream()
    assert stream_called.wait(timeout=2.0), "Worker did not call stream() in time"
    assert isinstance(captured_timeout[0], httpx.Timeout)
    assert captured_timeout[0].connect == 30.0
    assert captured_timeout[0].read == 600.0
    # Clean up: stop the streaming state
    if plugin._streaming_state:
        plugin._reset_request_state()
```

**Write new test** `test_handle_request_filter_applied` (does not exist yet; add to `TestHandleRequestAsync`):

```python
def test_handle_request_filter_applied(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
) -> None:
    """Filter rule is applied synchronously in handle_request() before thread start."""
    from flow_proxy_plugin.plugins.request_filter import FilterRule
    mock_svc.load_balancer.get_next_config.return_value = {
        "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
    }
    mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
    mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

    # Return a filter rule that removes a query param
    filter_rule = FilterRule(
        name="test-rule",
        matcher=lambda req, path: True,
        query_params_to_remove=["key"],
    )
    mock_svc.request_filter.find_matching_rule.return_value = filter_rule
    mock_svc.request_filter.filter_query_params.return_value = "/v1/messages"
    mock_svc.request_filter.get_headers_to_skip.return_value = set()

    request = Mock(spec=HttpParser)
    request.method = b"GET"
    request.path = b"/v1/messages?key=secret"
    request.headers = {}
    request.body = None
    request.buffer = None

    with patch.object(ProcessServices, "get", return_value=mock_svc):
        plugin.handle_request(request)

    # filter_query_params must be called — proves filter was applied before thread start
    mock_svc.request_filter.filter_query_params.assert_called_once()
    # Clean up
    if plugin._streaming_state:
        plugin._reset_request_state()
```

- [ ] **Step 4: Run full test suite after cleanup**

```bash
make test
```

Expected: all passed, no failures.

- [ ] **Step 5: Run linting**

```bash
make lint
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_web_server_plugin.py
git commit -m "test: remove obsolete synchronous streaming tests after B3 refactor"
```

---

### Task 7: Remove dead code from `web_server_plugin.py`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

The following methods are no longer called and can be removed:
- `_stream_response_body()` — replaced by worker + `read_from_descriptors`
- `_stream_bytes()` — replaced by worker
- `_stream_sse()` — replaced by worker
- `_log_stream_stats()` — logging now in `_streaming_worker` and `_finish_stream`
- `StreamStats` dataclass — no longer used

Also check `_send_response_headers()` — it is replaced by `_send_response_headers_from()`. Remove it if no longer called.

- [ ] **Step 1: Remove dead methods and `StreamStats`**

Delete from `web_server_plugin.py`:
- `StreamStats` dataclass (lines 30–66)
- `_stream_response_body()` method
- `_stream_bytes()` method
- `_stream_sse()` method
- `_log_stream_stats()` method
- `_send_response_headers()` method (replaced by `_send_response_headers_from`)
- `_prepare_headers()` method (deprecated wrapper, now dead code)

Remove the `import time` if it's now unused (check first).

- [ ] **Step 2: Run full test suite**

```bash
make test
```

Expected: all passed.

- [ ] **Step 3: Run linting**

```bash
make lint
```

- [ ] **Step 4: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py
git commit -m "refactor: remove dead synchronous streaming code after B3 async refactor"
```

---

### Task 8: Verify end-to-end behaviour

- [ ] **Step 1: Run the full test suite with coverage**

```bash
make test-cov
```

Expected: all passed, no regressions.

- [ ] **Step 2: Run pre-commit checks**

```bash
make check
```

Expected: all checks pass.

- [ ] **Step 3: Manual smoke test (optional but recommended)**

Start the proxy and send a streaming request:

```bash
make run
# In another terminal (direct URL — web server plugin, not forward proxy):
curl -N http://localhost:8899/flow-llm-proxy/v1/messages \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-3-5-sonnet-20241022","max_tokens":100,"stream":true,"messages":[{"role":"user","content":"Count to 5 slowly"}]}'
```

Expected: tokens appear progressively in the terminal, not all at once at the end.

Check logs for the pattern change: previously `SSE stream complete: first_byte=0ms, duration=Xms` (indicating buffering); now individual chunks should arrive at the client in real time.

- [ ] **Step 4: Final commit (if any minor fixes)**

```bash
git add -p  # stage only intentional changes
git commit -m "fix: <description of any final adjustments>"
```
