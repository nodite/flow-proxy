# Structured Request Logging Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc log lines in `FlowProxyWebServerPlugin` with a structured, permanent production logging protocol that includes TTFB, duration, bytes sent, stream mode, and end reason on every request.

**Architecture:** Add four fields to `StreamingState` (`start_time`, `stream`, `ttfb`, `bytes_sent`), rewrite the entry/backend/completion log lines in `web_server_plugin.py`, and make minor alignment changes in `jwt_generator.py` and `proxy_plugin.py`.

**Tech Stack:** Python 3.12+, pytest, proxy.py framework, httpx

---

## File Map

| File | Changes |
|------|---------|
| `flow_proxy_plugin/plugins/web_server_plugin.py` | Primary: StreamingState fields, handle_request, _streaming_worker, read_from_descriptors, _finish_stream, _reset_request_state |
| `flow_proxy_plugin/core/jwt_generator.py` | JWT log level INFO → DEBUG; f-string → %s style |
| `flow_proxy_plugin/plugins/proxy_plugin.py` | Reformat post-auth summary line |
| `tests/test_web_server_plugin.py` | Update all StreamingState() construction sites; add new log-format tests |

---

## Chunk 1: StreamingState fields + handle_request

### Task 1: Add `start_time` and `stream` to StreamingState

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:44-64` (StreamingState dataclass)
- Modify: `tests/test_web_server_plugin.py` (all `StreamingState(...)` calls at lines 29, 383, 530)

- [ ] **Step 1: Write failing test — new fields present on StreamingState**

Add to `TestDataStructures` in `tests/test_web_server_plugin.py`:

```python
def test_streaming_state_new_fields(self) -> None:
    import os, time, threading
    pipe_r, pipe_w = os.pipe()
    try:
        t = time.time()
        state = StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=queue.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="abc123",
            config_name="test-config",
            start_time=t,
            stream=True,
        )
        assert state.start_time == t
        assert state.stream is True
        assert state.ttfb is None
        assert state.bytes_sent == 0
    finally:
        os.close(pipe_r)
        os.close(pipe_w)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/oscaner/Projects/flow-proxy
poetry run pytest tests/test_web_server_plugin.py::TestDataStructures::test_streaming_state_new_fields -v
```

Expected: `TypeError: StreamingState.__init__() got an unexpected keyword argument 'start_time'`

- [ ] **Step 3: Add fields to StreamingState dataclass**

In `flow_proxy_plugin/plugins/web_server_plugin.py`, replace the `StreamingState` dataclass body (lines 54–64):

```python
    pipe_r: int  # registered with selector as readable fd
    pipe_w: int  # worker writes notification bytes here
    chunk_queue: "queue.Queue[_ResponseHeaders | bytes | None]"
    thread: threading.Thread | None  # None until assigned in handle_request()
    cancel: threading.Event  # set by _reset_request_state() on disconnect
    req_id: str  # for log context
    config_name: str  # for final access log line
    start_time: float  # set in handle_request() via time.time()
    stream: bool | None  # parsed from request body; None if indeterminate
    headers_sent: bool = False  # guards error-response logic
    is_sse: bool = False  # set by read_from_descriptors when _ResponseHeaders processed
    status_code: int = 0  # stored after _ResponseHeaders item is processed
    error: BaseException | None = None  # set by worker on exception
    ttfb: float | None = None  # set by worker on first chunk/line
    bytes_sent: int = 0  # incremented in read_from_descriptors() main thread only
```

Also update the `StreamingState` docstring to mention the new fields:

```python
    """Per-request streaming state for the B3 async pipe-mediated pattern.

    Stored as self._streaming_state. Initialized to None in __init__ and _rebind().
    Thread-safety: self.client is ONLY touched by the main thread.
    state.error is written by worker before queue.put(None) and read by main
    thread after queue.get() returns None — GIL guarantees visibility in CPython.
    Same GIL guarantee applies to state.ttfb (written by worker before sentinel,
    read by main thread after sentinel is dequeued).
    state.bytes_sent is written only from the main thread (read_from_descriptors).
    """
```

- [ ] **Step 4: Update existing StreamingState() calls in tests to pass new required fields**

Three sites in `tests/test_web_server_plugin.py`. Add `start_time=0.0, stream=None` to each:

Line 29 (`TestDataStructures.test_streaming_state_defaults`) — update constructor and add assertions for the new fields:
```python
            state = StreamingState(
                pipe_r=pipe_r,
                pipe_w=pipe_w,
                chunk_queue=queue.Queue(),
                thread=None,
                cancel=threading.Event(),
                req_id="abc123",
                config_name="test-config",
                start_time=0.0,
                stream=None,
            )
            assert state.headers_sent is False
            assert state.status_code == 0
            assert state.error is None
            assert state.ttfb is None        # new
            assert state.bytes_sent == 0     # new
```

Note: the existing assertions (`headers_sent`, `status_code`, `error`) stay in place; the two `# new` lines are appended.

Line 383 (`TestStreamingWorker._make_state`):
```python
        return StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=q.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="test01",
            config_name="cfg",
            start_time=0.0,
            stream=None,
        )
```

Line 530 (`TestEventLoopHooks._make_state_with_pipe`):
```python
        state = StreamingState(
            pipe_r=pipe_r, pipe_w=pipe_w,
            chunk_queue=q.Queue(),
            thread=None,
            cancel=threading.Event(),
            req_id="abc",
            config_name="cfg",
            start_time=0.0,
            stream=None,
        )
```

- [ ] **Step 5: Run all tests to verify they pass**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add start_time, stream, ttfb, bytes_sent fields to StreamingState"
```

---

### Task 2: Update handle_request() — entry line + stream parsing

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (`handle_request`, `_log_stream_mode`)
- Modify: `tests/test_web_server_plugin.py`

The goal: inline `stream` parsing into `handle_request()`, emit `→ METHOD path stream=X`, delete `_log_stream_mode()`, pass `start_time` and `stream` into `StreamingState`.

- [ ] **Step 1: Write failing test — entry line includes stream=**

Add to the existing `TestHandleRequestAsync` class in `tests/test_web_server_plugin.py`:

```python
def test_entry_log_includes_stream_true(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """handle_request emits '→ POST /path stream=True' when body has stream=true."""
    import logging
    request = MagicMock(spec=HttpParser)
    request.method = b"POST"
    request.path = b"/v1/messages"
    request.body = b'{"stream": true}'
    request.buffer = None
    request.headers = {}
    mock_svc.request_filter.find_matching_rule.return_value = None

    with (
        patch.object(ProcessServices, "get", return_value=mock_svc),
        patch.object(plugin, "_get_config_and_token", return_value=({"clientId": "cid"}, "test-cfg", "tok")),
        caplog.at_level(logging.INFO, logger="flow_proxy"),
    ):
        plugin.handle_request(request)

    # Filter to WS entry line only (not FWD line which also contains "→")
    entry_lines = [r.message for r in caplog.records if r.message.startswith("→ POST")]
    assert len(entry_lines) == 1
    assert "stream=True" in entry_lines[0]
    # Clean up worker thread
    if plugin._streaming_state:
        plugin._reset_request_state()
```

Add a companion test for `stream=None`:

```python
def test_entry_log_stream_none_when_no_body(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """handle_request emits 'stream=None' when body is absent."""
    import logging
    request = MagicMock(spec=HttpParser)
    request.method = b"GET"
    request.path = b"/v1/models"
    request.body = None
    request.buffer = None
    request.headers = {}
    mock_svc.request_filter.find_matching_rule.return_value = None

    with (
        patch.object(ProcessServices, "get", return_value=mock_svc),
        patch.object(plugin, "_get_config_and_token", return_value=({"clientId": "cid"}, "test-cfg", "tok")),
        caplog.at_level(logging.INFO, logger="flow_proxy"),
    ):
        plugin.handle_request(request)

    entry_lines = [r.message for r in caplog.records if r.message.startswith("→ GET")]
    assert len(entry_lines) == 1
    assert "stream=None" in entry_lines[0]
    if plugin._streaming_state:
        plugin._reset_request_state()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
poetry run pytest tests/test_web_server_plugin.py -k "test_entry_log" -v
```

Expected: FAIL (no `stream=` in current log line)

- [ ] **Step 3: Update handle_request() in web_server_plugin.py**

Add `import time` at the top of the file (if not already present).

In `handle_request()`, after `set_request_context(req_id, "WS")`:

Replace:
```python
        self.logger.info("→ %s %s", method, path)
```

With:
```python
        start_time = time.time()
        stream = self._parse_stream_field(request)
        self.logger.info("→ %s %s stream=%s", method, path, stream)
```

Add the helper method `_parse_stream_field` (near `_log_stream_mode`, which will be deleted):

```python
    @staticmethod
    def _parse_stream_field(request: HttpParser) -> bool | None:
        """Parse the 'stream' field from the JSON request body.

        Returns True/False if the field is present and parseable, None otherwise.
        All indeterminate cases (absent body, non-JSON, missing field) return None.
        """
        body = None
        if hasattr(request, "body") and request.body:
            body = request.body
        elif hasattr(request, "buffer") and request.buffer:
            body = bytes(request.buffer)
        if not body:
            return None
        try:
            parsed = json.loads(body)
            return parsed.get("stream")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
```

Pass `start_time` and `stream` into the `StreamingState` constructor. In `handle_request()`, update the `StreamingState(...)` call (around line 265–273):

```python
            state = StreamingState(
                pipe_r=pipe_r,
                pipe_w=pipe_w,
                chunk_queue=queue.Queue(),
                cancel=threading.Event(),
                req_id=req_id,
                config_name=config_name,
                thread=None,
                start_time=start_time,
                stream=stream,
            )
```

Delete the `_log_stream_mode()` method entirely, and remove the `self._log_stream_mode(body)` call in `handle_request()`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

Expected: all pass including the two new entry-log tests

- [ ] **Step 5: Run linters**

```bash
poetry run ruff check flow_proxy_plugin/plugins/web_server_plugin.py
poetry run mypy flow_proxy_plugin/plugins/web_server_plugin.py
```

Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: inline stream parsing into handle_request entry log line"
```

---

## Chunk 2: _streaming_worker — backend response line + TTFB

### Task 3: Replace two-line backend log with single TTFB line

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (`_streaming_worker`)
- Modify: `tests/test_web_server_plugin.py`

The goal:
- Remove `Backend response: %d %s...` pre-loop log call
- Remove `Received first SSE/chunk` log calls and `first_logged` flag
- On first non-empty chunk/line inside loop: set `state.ttfb`, emit `backend=STATUS transfer=X ttfb=Xs`
- Downgrade `Transport error` and `Worker error` from ERROR to WARNING

- [ ] **Step 1: Write failing test — backend line format**

```python
def test_worker_logs_backend_line_with_ttfb(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Worker emits 'backend=200 transfer=chunked ttfb=Xs' on first chunk."""
    import logging, os, time
    state = self._make_state()
    state.start_time = time.time() - 0.5  # simulate 500ms before first chunk
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.reason_phrase = "OK"
    mock_response.headers = httpx.Headers({"transfer-encoding": "chunked"})
    mock_response.iter_bytes.return_value = iter([b"hello"])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_svc.http_client.stream.return_value = mock_response

    with patch.object(ProcessServices, "get", return_value=mock_svc):
        with caplog.at_level(logging.INFO, logger="flow_proxy"):
            plugin._streaming_worker("POST", "https://example.com", {}, None, state)

    backend_lines = [r.message for r in caplog.records if r.message.startswith("backend=")]
    assert len(backend_lines) == 1
    assert "backend=200" in backend_lines[0]
    assert "transfer=chunked" in backend_lines[0]
    assert "ttfb=" in backend_lines[0]
    # Old lines must NOT appear
    old_lines = [r.message for r in caplog.records if "Backend response:" in r.message]
    assert old_lines == []
    old_first_chunk = [r.message for r in caplog.records if "Received first" in r.message]
    assert old_first_chunk == []
    os.close(state.pipe_r)
    os.close(state.pipe_w)
```

Add a test that `state.ttfb` is set after the worker runs:

```python
def test_worker_sets_state_ttfb(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
) -> None:
    """Worker sets state.ttfb after receiving the first chunk."""
    import os, time
    state = self._make_state()
    state.start_time = time.time()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.reason_phrase = "OK"
    mock_response.headers = httpx.Headers({})
    mock_response.iter_bytes.return_value = iter([b"data"])
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_svc.http_client.stream.return_value = mock_response

    with patch.object(ProcessServices, "get", return_value=mock_svc):
        plugin._streaming_worker("GET", "https://example.com", {}, None, state)

    assert state.ttfb is not None
    assert state.ttfb >= 0.0
    os.close(state.pipe_r)
    os.close(state.pipe_w)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
poetry run pytest tests/test_web_server_plugin.py -k "test_worker_logs_backend_line or test_worker_sets_state_ttfb" -v
```

Expected: FAIL

- [ ] **Step 3: Rewrite _streaming_worker in web_server_plugin.py**

In `_streaming_worker`, make these changes:

1. **Delete** the pre-loop `self.logger.info("Backend response: %d %s...", ...)` block (currently lines 468–474).
2. **Delete** `first_logged = False` and all `if not first_logged:` blocks with their associated log calls.
3. **Inside the `else:` (non-SSE) loop**, on the first non-empty chunk, set ttfb and emit the backend line before `chunk_queue.put`:

```python
                else:
                    for chunk in response.iter_bytes():
                        if state.cancel.is_set():
                            break
                        if not chunk:
                            continue
                        if state.ttfb is None:
                            state.ttfb = time.time() - state.start_time
                            transfer = response.headers.get("transfer-encoding", "none")
                            self.logger.info(
                                "backend=%d transfer=%s ttfb=%.1fs",
                                response.status_code,
                                transfer,
                                state.ttfb,
                            )
                        state.chunk_queue.put(chunk)
                        try:
                            os.write(state.pipe_w, b"\x00")
                        except OSError:
                            return
```

4. **Inside the `if is_sse:` loop**, similarly:

```python
                if is_sse:
                    for line in response.iter_lines():
                        if state.cancel.is_set():
                            break
                        chunk = self._encode_sse_line(line)
                        if chunk:
                            if state.ttfb is None:
                                state.ttfb = time.time() - state.start_time
                                transfer = response.headers.get("transfer-encoding", "none")
                                self.logger.info(
                                    "backend=%d transfer=%s ttfb=%.1fs",
                                    response.status_code,
                                    transfer,
                                    state.ttfb,
                                )
                            state.chunk_queue.put(chunk)
                            try:
                                os.write(state.pipe_w, b"\x00")
                            except OSError:
                                return
```

5. **Downgrade** `self.logger.error("Transport error — marking httpx client dirty: %s", e)` to `self.logger.warning(...)`.
6. **Downgrade** `self.logger.error("Worker error: %s", e, exc_info=True)` to `self.logger.warning(...)`.

Add `import time` at the top of the file if not already present.

- [ ] **Step 4: Run all tests**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

Expected: all pass

- [ ] **Step 5: Run linters**

```bash
poetry run ruff check flow_proxy_plugin/plugins/web_server_plugin.py
poetry run mypy flow_proxy_plugin/plugins/web_server_plugin.py
```

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: replace two-line backend log with single ttfb line in streaming worker"
```

---

## Chunk 3: bytes_sent + completion lines

### Task 4: Track bytes_sent in read_from_descriptors

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (`read_from_descriptors`)
- Modify: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing test**

```python
def test_read_from_descriptors_tracks_bytes_sent(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """bytes_sent accumulates payload bytes; header bytes are excluded."""
    import asyncio, os
    state, pipe_r, pipe_w = self._make_state_with_pipe()
    plugin._streaming_state = state
    plugin.client = MagicMock()

    # Pre-load queue: headers item then two byte chunks
    state.chunk_queue.put(_ResponseHeaders(200, "OK", {}, False))
    state.chunk_queue.put(b"hello")
    state.chunk_queue.put(b"world!")

    os.write(pipe_w, b"\x00")  # trigger read
    try:
        asyncio.run(plugin.read_from_descriptors([pipe_r]))
    finally:
        plugin._streaming_state = None
        os.close(pipe_r)
        os.close(pipe_w)

    assert state.bytes_sent == len(b"hello") + len(b"world!")  # 11
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/test_web_server_plugin.py::TestEventLoopHooks::test_read_from_descriptors_tracks_bytes_sent -v
```

Expected: FAIL (`state.bytes_sent == 0`)

- [ ] **Step 3: Add bytes_sent tracking in read_from_descriptors**

In `read_from_descriptors`, in the `else: # bytes chunk` branch, add the counter before `self.client.queue()`:

```python
            else:  # bytes chunk
                state.bytes_sent += len(item)
                self.client.queue(memoryview(item))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

- [ ] **Step 5: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: track bytes_sent in read_from_descriptors"
```

---

### Task 5: Structured completion line in _finish_stream

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (`_finish_stream`)
- Modify: `tests/test_web_server_plugin.py`

No new helper method is needed — all rendering logic is inlined directly into `_finish_stream` (Step 3 below). `import httpx` and `import time` are already at the top of the file.

- [ ] **Step 1: Write failing tests**

```python
def test_finish_stream_ok_emits_structured_completion_line(
    self, plugin: FlowProxyWebServerPlugin, caplog: pytest.LogCaptureFixture
) -> None:
    """_finish_stream emits '← 200 [cfg] stream=True ttfb=... end=ok' on success."""
    import logging, os, time
    state, pipe_r, pipe_w = self._make_state_with_pipe()
    state.start_time = time.time() - 5.0
    state.stream = True
    state.ttfb = 1.2
    state.bytes_sent = 1024
    state.status_code = 200
    state.headers_sent = True
    plugin._streaming_state = state
    plugin.client = MagicMock()

    with caplog.at_level(logging.INFO, logger="flow_proxy"):
        plugin._finish_stream(state)

    lines = [r.message for r in caplog.records if r.message.startswith("← ")]
    assert len(lines) == 1
    line = lines[0]
    assert "← 200 [cfg]" in line
    assert "stream=True" in line
    assert "ttfb=1.2s" in line
    assert "bytes=1024" in line
    assert "end=ok" in line
    # _finish_stream closes both fds; re-closing must raise OSError
    with pytest.raises(OSError):
        os.close(pipe_r)
    with pytest.raises(OSError):
        os.close(pipe_w)

def test_finish_stream_transport_error_emits_warning(
    self, plugin: FlowProxyWebServerPlugin, caplog: pytest.LogCaptureFixture
) -> None:
    """_finish_stream emits WARNING with end=transport_error when state.error is TransportError.

    state.headers_sent=False causes _send_error(503) to be called (client.queue mock absorbs it).
    """
    import logging, os, time
    state, pipe_r, pipe_w = self._make_state_with_pipe()
    state.start_time = time.time() - 10.0
    state.stream = False
    state.ttfb = None
    state.bytes_sent = 0
    state.status_code = 0
    state.headers_sent = False
    state.error = httpx.RemoteProtocolError("peer closed")
    plugin._streaming_state = state
    plugin.client = MagicMock()

    with caplog.at_level(logging.WARNING, logger="flow_proxy"):
        plugin._finish_stream(state)

    completion = [r for r in caplog.records if r.message.startswith("← ")]
    assert len(completion) == 1
    assert completion[0].levelname == "WARNING"
    assert "end=transport_error" in completion[0].message
    assert "ttfb=-" in completion[0].message
    # Old "Stream ended with error" line must NOT appear
    old = [r for r in caplog.records if "Stream ended with error" in r.message]
    assert old == []
    # _finish_stream closes both fds; re-closing must raise OSError
    with pytest.raises(OSError):
        os.close(pipe_r)
    with pytest.raises(OSError):
        os.close(pipe_w)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
poetry run pytest tests/test_web_server_plugin.py -k "test_finish_stream" -v
```

- [ ] **Step 3: Rewrite _finish_stream in web_server_plugin.py**

Replace the current `_finish_stream` body with:

```python
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
            elif state.is_sse:
                self._send_sse_error_event()
            # non-SSE + headers sent: silent close

            if isinstance(state.error, httpx.TransportError):
                end = "transport_error"
            else:
                end = "worker_error"
            status = str(state.status_code) if state.status_code else "---"
            ttfb_str = ("%.1fs" % state.ttfb) if state.ttfb is not None else "-"
            duration = time.time() - state.start_time
            self.logger.warning(
                "← %s [%s] stream=%s ttfb=%s duration=%.1fs bytes=%d end=%s",
                status, state.config_name, state.stream,
                ttfb_str, duration, state.bytes_sent, end,
            )
        else:
            status = str(state.status_code) if state.status_code else "---"
            ttfb_str = ("%.1fs" % state.ttfb) if state.ttfb is not None else "-"
            duration = time.time() - state.start_time
            log_func = (
                self.logger.info if state.status_code < 400 else self.logger.warning
            )
            log_func(
                "← %s [%s] stream=%s ttfb=%s duration=%.1fs bytes=%d end=ok",
                status, state.config_name, state.stream,
                ttfb_str, duration, state.bytes_sent,
            )
        clear_request_context()
```

- [ ] **Step 4: Run all tests**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

Expected: all pass

- [ ] **Step 5: Run linters**

```bash
poetry run ruff check flow_proxy_plugin/plugins/web_server_plugin.py
poetry run mypy flow_proxy_plugin/plugins/web_server_plugin.py
```

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: emit structured completion line in _finish_stream"
```

---

### Task 6: Structured completion line in _reset_request_state

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (`_reset_request_state`)
- Modify: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing test**

```python
def test_reset_request_state_emits_client_disconnected_line(
    self, plugin: FlowProxyWebServerPlugin, caplog: pytest.LogCaptureFixture
) -> None:
    """_reset_request_state emits '← 200 [cfg] ... end=client_disconnected' at INFO."""
    import logging, os, time
    state, pipe_r, pipe_w = self._make_state_with_pipe()
    state.start_time = time.time() - 8.0
    state.stream = True
    state.ttfb = 2.0
    state.bytes_sent = 512
    state.status_code = 200
    plugin._streaming_state = state

    # Assign a dummy thread so join() doesn't fail
    import threading
    state.thread = threading.Thread(target=lambda: None)
    state.thread.start()

    with caplog.at_level(logging.INFO, logger="flow_proxy"):
        plugin._reset_request_state()

    lines = [r.message for r in caplog.records if r.message.startswith("← ")]
    assert len(lines) == 1
    line = lines[0]
    assert "← 200 [cfg]" in line
    assert "stream=True" in line
    assert "ttfb=2.0s" in line
    assert "bytes=512" in line
    assert "end=client_disconnected" in line
    # Old freeform line must NOT appear
    old = [r for r in caplog.records if "Stream canceled" in r.message]
    assert old == []
    # _reset_request_state closes both fds; re-closing must raise OSError
    with pytest.raises(OSError):
        os.close(pipe_r)
    with pytest.raises(OSError):
        os.close(pipe_w)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/test_web_server_plugin.py -k "test_reset_request_state_emits" -v
```

- [ ] **Step 3: Update _reset_request_state in web_server_plugin.py**

Replace the body after the `if state is None: return` guard:

```python
    def _reset_request_state(self) -> None:
        """Cancel and join worker thread; close pipe fds. Called by PluginPool.release()."""
        state = self._streaming_state
        if state is None:
            return
        set_request_context(state.req_id, "WS")
        state.cancel.set()
        if state.thread is not None:
            state.thread.join(timeout=2.0)
        for fd in (state.pipe_r, state.pipe_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._streaming_state = None

        status = str(state.status_code) if state.status_code else "---"
        ttfb_str = ("%.1fs" % state.ttfb) if state.ttfb is not None else "-"
        duration = time.time() - state.start_time
        self.logger.info(
            "← %s [%s] stream=%s ttfb=%s duration=%.1fs bytes=%d end=client_disconnected",
            status, state.config_name, state.stream,
            ttfb_str, duration, state.bytes_sent,
        )
        clear_request_context()
```

- [ ] **Step 4: Run all tests**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

- [ ] **Step 5: Run full test suite**

```bash
poetry run pytest -v
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: emit structured completion line in _reset_request_state"
```

---

## Chunk 4: FWD format + JWT level + proxy_plugin alignment

### Task 7: FWD log line format change

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (`handle_request`)
- Modify: `tests/test_web_server_plugin.py`

- [ ] **Step 1: Write failing test**

```python
def test_fwd_log_uses_arrow_format(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """handle_request emits '→ https://...' for the FWD line, not 'Sending request to backend:'."""
    import logging
    request = MagicMock(spec=HttpParser)
    request.method = b"POST"
    request.path = b"/v1/messages"
    request.body = b'{}'
    request.buffer = None
    request.headers = {}
    mock_svc.request_filter.find_matching_rule.return_value = None

    with (
        patch.object(ProcessServices, "get", return_value=mock_svc),
        patch.object(plugin, "_get_config_and_token", return_value=({"clientId": "cid"}, "test-cfg", "tok")),
        caplog.at_level(logging.INFO, logger="flow_proxy"),
    ):
        plugin.handle_request(request)

    fwd_lines = [r.message for r in caplog.records if "flow.ciandt.com" in r.message]
    assert len(fwd_lines) == 1
    assert fwd_lines[0].startswith("→ ")
    old_lines = [r.message for r in caplog.records if "Sending request to backend:" in r.message]
    assert old_lines == []
    if plugin._streaming_state:
        plugin._reset_request_state()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/test_web_server_plugin.py -k "test_fwd_log" -v
```

- [ ] **Step 3: Update FWD log line in handle_request**

In `handle_request()`, within the `with component_context("FWD"):` block, replace:
```python
            self.logger.info("Sending request to backend: %s", target_url)
```
with:
```python
            self.logger.info("→ %s", target_url)
```

- [ ] **Step 4: Run all tests**

```bash
poetry run pytest tests/ -v
```

- [ ] **Step 5: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: change FWD log line to arrow format"
```

---

### Task 8: JWT log level INFO → DEBUG

**Files:**
- Modify: `flow_proxy_plugin/core/jwt_generator.py`
- Modify: `tests/test_jwt_generator.py`

- [ ] **Step 1: Write failing test**

In `tests/test_jwt_generator.py`, add:

```python
def test_generate_token_does_not_log_at_info(
    self, caplog: pytest.LogCaptureFixture
) -> None:
    """JWT generation does not emit INFO log (clientId should not appear at INFO level).

    JWTGenerator uses jwt.encode directly — no HTTP calls. No patching needed.
    Add this to the existing TestJWTGenerator class which already calls
    setup_method to clear cache before each test.
    """
    import logging
    gen = JWTGenerator()
    config = {"clientId": "test-id", "clientSecret": "secret", "tenant": "tenant"}
    with caplog.at_level(logging.INFO):
        gen.generate_token(config)
    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO and "test-id" in r.message]
    assert info_msgs == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/test_jwt_generator.py -k "test_generate_token_does_not_log_at_info" -v
```

- [ ] **Step 3: Update jwt_generator.py**

In `flow_proxy_plugin/core/jwt_generator.py`, change line 69:
```python
        self.logger.info(f"Generated JWT token for {client_id}")
```
to:
```python
        self.logger.debug("Generated JWT token for %s", client_id)
```

- [ ] **Step 4: Run all tests**

```bash
poetry run pytest tests/ -v
```

- [ ] **Step 5: Commit**

```bash
git add flow_proxy_plugin/core/jwt_generator.py tests/test_jwt_generator.py
git commit -m "feat: downgrade JWT token generation log from INFO to DEBUG"
```

---

### Task 9: proxy_plugin post-auth summary line

**Files:**
- Modify: `flow_proxy_plugin/plugins/proxy_plugin.py` (`before_upstream_connection`)
- Modify: `tests/test_plugin.py`

- [ ] **Step 1: Write failing test**

Add to `TestFlowProxyPluginRequestProcessing` in `tests/test_plugin.py` (uses the same `plugin` fixture already defined in that class):

```python
def test_proxy_plugin_log_format(
    self, plugin: FlowProxyPlugin, caplog: pytest.LogCaptureFixture
) -> None:
    """before_upstream_connection emits '→ POST /path [config]' format."""
    import logging
    request = Mock(spec=HttpParser)
    request.method = b"POST"
    request.path = b"/v1/messages"
    request.headers = {}

    with (
        patch.object(plugin.request_forwarder, "validate_request", return_value=True),
        patch.object(plugin.jwt_generator, "generate_token", return_value="tok"),
        patch.object(plugin.request_forwarder, "modify_request_headers", return_value=request),
        caplog.at_level(logging.INFO, logger="flow_proxy"),
    ):
        plugin.before_upstream_connection(request)

    summary = [r.message for r in caplog.records if r.message.startswith("→ ") and "[" in r.message]
    assert len(summary) == 1
    assert summary[0].startswith("→ POST")
    assert "/v1/messages" in summary[0]
    old = [r for r in caplog.records if "Request processed with config" in r.message]
    assert old == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/test_plugin.py -k "test_proxy_plugin_log_format" -v
```

- [ ] **Step 3: Update before_upstream_connection in proxy_plugin.py**

In `before_upstream_connection()` (lines 116–120), the current "Log success" block is:
```python
            # Log success
            target_url = self._decode_bytes(request.path) if request.path else "unknown"
            self.logger.info(
                "Request processed with config '%s' → %s", config_name, target_url
            )
```

Replace it with (the `target_url` variable below is no longer used for logging; the comment "Log success" can be kept or removed):
```python
            # Log success
            method = self._decode_bytes(request.method) if request.method else "GET"
            path = self._decode_bytes(request.path) if request.path else "unknown"
            self.logger.info("→ %s %s [%s]", method, path, config_name)
```

Note: `target_url` was previously built from `request.path`, not the full URL — the variable name was misleading. The new `path` variable is equivalent. No other callers of `target_url` exist in the method.

- [ ] **Step 4: Run all tests**

```bash
poetry run pytest tests/ -v
```

- [ ] **Step 5: Run full linting**

```bash
make lint
```

Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/proxy_plugin.py tests/test_plugin.py
git commit -m "feat: align proxy_plugin post-auth log line to arrow format"
```

---

### Task 10: Final verification

- [ ] **Step 1: Run full test suite with coverage**

```bash
make test-cov
```

Expected: all tests pass, no regressions

- [ ] **Step 2: Run all linters**

```bash
make lint
```

Expected: ruff, mypy, pylint all pass

- [ ] **Step 3: Smoke-check log output**

Start the proxy and issue one request. Verify the log output matches the spec format:

```bash
poetry run flow-proxy --port 8899 --log-level DEBUG &
curl -s http://127.0.0.1:8899/v1/models
```

Verify in `logs/flow_proxy_plugin.log`:
- `→ GET /v1/models stream=None` (entry line)
- `[FWD] → https://flow.ciandt.com/...` (FWD line)
- `backend=NNN transfer=... ttfb=...` (backend line, inside loop)
- `← NNN [config] stream=None ttfb=... duration=... bytes=... end=ok/transport_error` (completion line)
- No `Sending request to backend:` lines
- No `Received first chunk/SSE line` lines
- No `Stream ended with error:` lines
- No `Stream canceled` lines
- JWT line absent at INFO (only visible at DEBUG)

- [ ] **Step 4: Final commit if smoke check passes**

```bash
git add -p  # stage any fixups
git commit -m "chore: final structured logging verification"
```
