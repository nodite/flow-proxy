# SSE Streaming Optimization Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `FlowProxyWebServerPlugin` streaming to split SSE/non-SSE paths, add `StreamStats` for TTFT/duration/event observability, and remove the per-chunk `_is_client_connected()` pre-check overhead.

**Architecture:** Add a `StreamStats` dataclass to track timing and event metrics; refactor `_stream_response_body` into a dispatcher that calls `_stream_sse` (using `iter_lines()`) or `_stream_bytes` (using `iter_bytes()`); remove `_is_client_connected()` entirely in favor of exception-based disconnect detection already present in both paths.

**Tech Stack:** Python 3.12, httpx, proxy.py, pytest, dataclasses

---

## File Map

| File | Change |
|------|--------|
| `flow_proxy_plugin/plugins/web_server_plugin.py` | Add `StreamStats` dataclass; add `_stream_sse`, `_stream_bytes`, `_log_stream_stats`; refactor `_stream_response_body` dispatcher; remove `_is_client_connected` |
| `tests/test_web_server_plugin.py` | Extend `make_mock_httpx_response` with `lines` param; delete `TestConnectionStateCheck`; update `TestStreamResponseBody`; add new SSE stats tests; clean up `mock_plugin_args` fixture |

---

## Chunk 1: `StreamStats` Dataclass + Test Helper

### Task 1: `StreamStats` unit tests

**Files:**
- Modify: `tests/test_web_server_plugin.py` (add `TestStreamStats` class after imports)

- [ ] **Step 1: Write failing tests for `StreamStats`**

Add this class after the existing imports in `tests/test_web_server_plugin.py`, before any existing test class:

```python
from dataclasses import dataclass  # already imported via web_server_plugin? No — add direct import if needed
from flow_proxy_plugin.plugins.web_server_plugin import StreamStats


class TestStreamStats:
    """Unit tests for StreamStats dataclass."""

    def test_ttft_ms_none_when_no_first_chunk(self) -> None:
        stats = StreamStats(start_time=1000.0)
        assert stats.ttft_ms is None

    def test_ttft_ms_calculated_correctly(self) -> None:
        stats = StreamStats(start_time=1000.0, first_chunk_time=1000.042)
        assert abs(stats.ttft_ms - 42.0) < 0.001  # type: ignore[operator]

    def test_duration_ms_none_when_no_first_chunk(self) -> None:
        stats = StreamStats(start_time=1000.0, end_time=1001.0)
        assert stats.duration_ms is None

    def test_duration_ms_none_when_no_end_time(self) -> None:
        stats = StreamStats(start_time=1000.0, first_chunk_time=1000.042)
        assert stats.duration_ms is None

    def test_duration_ms_calculated_correctly(self) -> None:
        stats = StreamStats(
            start_time=1000.0,
            first_chunk_time=1000.042,
            end_time=1003.292,
        )
        assert abs(stats.duration_ms - 3250.0) < 0.001  # type: ignore[operator]

    def test_defaults(self) -> None:
        stats = StreamStats(start_time=0.0)
        assert stats.bytes_sent == 0
        assert stats.chunks_sent == 0
        assert stats.event_count == 0
        assert stats.completed is False
```

- [ ] **Step 2: Run tests to confirm they fail (ImportError expected)**

```bash
pytest tests/test_web_server_plugin.py::TestStreamStats -v
```

Expected: `ImportError: cannot import name 'StreamStats'`

### Task 2: Implement `StreamStats` dataclass

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (add after imports, before class definition)

- [ ] **Step 3: Add `StreamStats` and required imports**

Add `import time` and `from dataclasses import dataclass, field` to the imports section of `web_server_plugin.py`. Then add the dataclass after the module-level pool variables (after `_WEB_POOL_SIZE = ...`):

```python
@dataclass
class StreamStats:
    """Metrics collected during a single streaming response."""

    start_time: float
    first_chunk_time: float | None = None
    end_time: float | None = None
    bytes_sent: int = 0
    chunks_sent: int = 0  # non-empty payload writes only
    event_count: int = 0  # SSE only: incremented at each empty-line boundary
    completed: bool = False  # True only if stream finished without interruption

    @property
    def ttft_ms(self) -> float | None:
        """Time-to-first-token in milliseconds, or None if no data received."""
        if self.first_chunk_time is None:
            return None
        return (self.first_chunk_time - self.start_time) * 1000

    @property
    def duration_ms(self) -> float | None:
        """Stream duration (first chunk → last chunk) in ms, or None if incomplete."""
        if self.end_time is None or self.first_chunk_time is None:
            return None
        return (self.end_time - self.first_chunk_time) * 1000
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_web_server_plugin.py::TestStreamStats -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: all existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add StreamStats dataclass for SSE streaming metrics"
```

---

### Task 3: Extend `make_mock_httpx_response` with `lines` parameter

**Files:**
- Modify: `tests/test_web_server_plugin.py` — update `make_mock_httpx_response` helper (around line 81)

- [ ] **Step 7: Update the helper function signature and body**

Find `make_mock_httpx_response` (currently around line 81) and update it:

```python
def make_mock_httpx_response(
    status_code: int = 200,
    reason_phrase: str = "OK",
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
    lines: list[str] | None = None,
) -> Mock:
    """Create a mock httpx.Response for testing.

    Use `chunks` for non-SSE iter_bytes() tests.
    Use `lines` for SSE iter_lines() tests (str values, empty string = event boundary).
    """
    response = Mock(spec=httpx.Response)
    response.status_code = status_code
    response.reason_phrase = reason_phrase
    response.headers = httpx.Headers(headers or {"content-type": "application/json"})
    response.iter_bytes.return_value = iter(chunks or [])
    response.iter_lines.return_value = iter(lines or [])
    return response
```

- [ ] **Step 8: Run tests to confirm no regressions**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: all tests PASS (additive change only).

- [ ] **Step 9: Commit**

```bash
git add tests/test_web_server_plugin.py
git commit -m "test: extend make_mock_httpx_response with lines param for SSE tests"
```

---

## Chunk 2: `_stream_bytes` Extraction

### Task 4: Write updated tests for `_stream_bytes` behavior

The current `_stream_response_body` will become `_stream_bytes(response, stats)` internally. The tests in `TestStreamResponseBody` test the public `_stream_response_body` interface, which will change its return type from `tuple[int, int]` to `StreamStats`. We update these tests now so that when we refactor the implementation they pass.

**Files:**
- Modify: `tests/test_web_server_plugin.py` — update `TestStreamResponseBody` tests

- [ ] **Step 10: Update `test_forwards_all_chunks_immediately`**

Replace the existing test body (currently around line 316):

```python
def test_forwards_all_chunks_immediately(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """Each chunk from iter_bytes() is queued immediately — no buffering."""
    chunks = [b"data: token1\n\n", b"data: token2\n\n", b"data: [DONE]\n\n"]
    mock_response = make_mock_httpx_response(chunks=chunks)

    queued_chunks: list[bytes] = []
    plugin.client = Mock(queue=lambda mv: queued_chunks.append(bytes(mv)))

    stats = plugin._stream_response_body(mock_response)

    assert queued_chunks == chunks
    assert stats.chunks_sent == 3
    assert stats.bytes_sent == sum(len(c) for c in chunks)
    assert stats.completed is True
    assert stats.event_count == 0  # non-SSE path never increments event_count
```

- [ ] **Step 11: Update `test_stops_when_client_disconnects`**

Replace the existing test body (currently around line 333):

```python
def test_stops_when_client_disconnects(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """Streaming stops gracefully when client disconnects on first write."""
    chunks = [b"chunk1", b"chunk2", b"chunk3"]
    mock_response = make_mock_httpx_response(chunks=chunks)

    plugin.client = Mock(queue=Mock(side_effect=BrokenPipeError()))

    stats = plugin._stream_response_body(mock_response)

    assert stats.bytes_sent == 0
    assert stats.completed is False
    assert stats.end_time is not None
```

- [ ] **Step 12: Update `test_handles_broken_pipe_gracefully`**

Replace the existing test body (currently around line 347):

```python
def test_handles_broken_pipe_gracefully(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """BrokenPipeError during streaming exits cleanly without raising."""
    chunks = [b"chunk1", b"chunk2"]
    mock_response = make_mock_httpx_response(chunks=chunks)

    call_count = 0

    def queue_with_error(mv: memoryview) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise BrokenPipeError()

    plugin.client = Mock(queue=queue_with_error)

    stats = plugin._stream_response_body(mock_response)  # must not raise
    assert stats.completed is False
```

- [ ] **Step 13: Update `test_handles_connection_reset_error_gracefully`**

```python
def test_handles_connection_reset_error_gracefully(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """ConnectionResetError during streaming exits cleanly without raising."""
    chunks = [b"chunk1", b"chunk2"]
    mock_response = make_mock_httpx_response(chunks=chunks)

    call_count = 0

    def queue_with_reset(mv: memoryview) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise ConnectionResetError()

    plugin.client = Mock(queue=queue_with_reset)

    stats = plugin._stream_response_body(mock_response)  # must not raise
    assert stats.completed is False
```

- [ ] **Step 14: Update `test_handles_os_error_errno_32_gracefully`**

```python
def test_handles_os_error_errno_32_gracefully(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """OSError with errno=32 (broken pipe) exits cleanly without raising."""
    chunks = [b"chunk1", b"chunk2"]
    mock_response = make_mock_httpx_response(chunks=chunks)

    call_count = 0

    def queue_with_oserror(mv: memoryview) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            err = OSError("Broken pipe")
            err.errno = 32
            raise err

    plugin.client = Mock(queue=queue_with_oserror)

    stats = plugin._stream_response_body(mock_response)  # must not raise
    assert stats.completed is False
```

- [ ] **Step 15: Update `test_skips_empty_chunks`**

```python
def test_skips_empty_chunks(self, plugin: FlowProxyWebServerPlugin) -> None:
    """Empty byte strings from iter_bytes() are skipped, not forwarded."""
    chunks = [b"chunk1", b"", b"chunk2"]
    mock_response = make_mock_httpx_response(chunks=chunks)

    queued_chunks: list[bytes] = []
    plugin.client = Mock(queue=lambda mv: queued_chunks.append(bytes(mv)))

    stats = plugin._stream_response_body(mock_response)

    assert queued_chunks == [b"chunk1", b"chunk2"]
    assert stats.chunks_sent == 2
```

- [ ] **Step 16: Run updated tests to confirm they FAIL (return type mismatch)**

```bash
pytest tests/test_web_server_plugin.py::TestStreamResponseBody -v
```

Expected: multiple FAILs — `cannot unpack non-sequence StreamStats` or `AttributeError: 'tuple' has no attribute 'chunks_sent'` — confirms tests are waiting for the refactor.

### Task 5: Implement `_stream_bytes` and refactor `_stream_response_body`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

- [ ] **Step 17: Add `_stream_bytes` method**

Add this new private method to `FlowProxyWebServerPlugin`, after `_stream_response_body` (the old one, which we will replace next):

```python
def _stream_bytes(self, response: httpx.Response, stats: StreamStats) -> None:
    """Stream non-SSE response body to client, updating stats in place.

    Sets stats.completed = True only if the iterator is exhausted without
    interruption. Returns early (completed stays False) on disconnect.
    """
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        if stats.first_chunk_time is None:
            stats.first_chunk_time = time.perf_counter()
            self.logger.info(
                "Received first chunk from backend: %d bytes", len(chunk)
            )
        try:
            self.client.queue(memoryview(chunk))
            stats.bytes_sent += len(chunk)
            stats.chunks_sent += 1
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if isinstance(e, OSError) and e.errno != 32:
                self.logger.warning("OS error during streaming: %s", e)
            else:
                self.logger.debug(
                    "Client disconnected during streaming - sent %d bytes",
                    stats.bytes_sent,
                )
            return  # completed stays False
    stats.completed = True  # loop exhausted cleanly
```

- [ ] **Step 18: Add `_log_stream_stats` method**

Add this method after `_stream_bytes`:

```python
def _log_stream_stats(self, stats: StreamStats, is_sse: bool) -> None:
    """Log a single summary line after streaming completes or is interrupted."""
    if is_sse:
        prefix = "SSE stream complete" if stats.completed else "SSE stream interrupted"
        if stats.ttft_ms is not None:
            self.logger.info(
                "%s: TTFT=%.0fms, duration=%.0fms, events=%d, bytes=%d",
                prefix,
                stats.ttft_ms,
                stats.duration_ms or 0.0,
                stats.event_count,
                stats.bytes_sent,
            )
        else:
            self.logger.info(
                "%s: no data received, bytes=%d", prefix, stats.bytes_sent
            )
    else:
        prefix = "Stream complete" if stats.completed else "Stream interrupted"
        self.logger.info(
            "%s: bytes=%d, chunks=%d", prefix, stats.bytes_sent, stats.chunks_sent
        )
```

- [ ] **Step 19: Replace `_stream_response_body` with dispatcher**

Replace the existing `_stream_response_body` method body (currently lines 321–366 of `web_server_plugin.py`) with:

```python
def _stream_response_body(self, response: httpx.Response) -> StreamStats:
    """Stream response body to client; returns stats for the completed stream.

    stats.completed is set inside _stream_bytes/_stream_sse only when the
    iterator exhausts cleanly. The dispatcher does not set it — this lets
    disconnect-interrupted streams correctly have completed=False.
    """
    is_sse = "text/event-stream" in response.headers.get("content-type", "")
    stats = StreamStats(start_time=time.perf_counter())
    try:
        if is_sse:
            self._stream_sse(response, stats)
        else:
            self._stream_bytes(response, stats)
    finally:
        stats.end_time = time.perf_counter()
        self._log_stream_stats(stats, is_sse)
    return stats
```

Note: `_stream_sse` does not exist yet — we will add it in Task 6. For now this will cause a `AttributeError` if an SSE response arrives, but the non-SSE path and all existing tests use non-SSE content types, so the test suite should pass.

- [ ] **Step 20: Run `TestStreamResponseBody` to confirm non-SSE tests pass**

```bash
pytest tests/test_web_server_plugin.py::TestStreamResponseBody -v
```

Expected: all 6 updated tests PASS (they all use non-SSE mocks → go through `_stream_bytes`).

- [ ] **Step 21: Run full test suite**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: all existing tests PASS (except `TestConnectionStateCheck` which still tests the not-yet-removed `_is_client_connected` — it should still pass since the method still exists at this point).

- [ ] **Step 22: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "refactor: extract _stream_bytes and dispatcher _stream_response_body with StreamStats"
```

---

## Chunk 3: `_stream_sse` New Method

### Task 6: Write SSE stats tests

**Files:**
- Modify: `tests/test_web_server_plugin.py` — add `TestSseStreamStats` class after `TestStreamResponseBody`

- [ ] **Step 23: Write failing SSE tests**

Add this new test class after `TestStreamResponseBody`:

```python
class TestSseStreamStats:
    """Tests for _stream_response_body with SSE (text/event-stream) responses."""

    def test_sse_stream_stats_event_count(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Empty lines in iter_lines() mark event boundaries and increment event_count."""
        # Two SSE events: "data: tok1\n\n" and "data: tok2\n\n"
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.event_count == 2
        assert stats.completed is True

    def test_sse_stream_stats_chunks_sent_excludes_separators(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """chunks_sent counts non-empty lines only (not blank event separators)."""
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.chunks_sent == 2  # two non-empty lines

    def test_sse_stream_stats_bytes_sent_includes_separators(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """bytes_sent includes both data lines and blank separator newlines."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        stats = plugin._stream_response_body(mock_response)

        total_queued = sum(len(b) for b in queued)
        assert stats.bytes_sent == total_queued

    def test_sse_stream_stats_ttft(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """ttft_ms measures time from start_time to first non-empty line."""
        lines = ["data: tok1", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        time_values = [1000.0, 1000.042, 1003.292]
        call_index = 0

        def mock_perf_counter() -> float:
            nonlocal call_index
            val = time_values[call_index % len(time_values)]
            call_index += 1
            return val

        with patch("flow_proxy_plugin.plugins.web_server_plugin.time") as mock_time:
            mock_time.perf_counter.side_effect = mock_perf_counter
            stats = plugin._stream_response_body(mock_response)

        assert stats.ttft_ms is not None
        assert abs(stats.ttft_ms - 42.0) < 1.0

    def test_sse_stream_stats_duration(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """duration_ms is non-None and positive after a complete SSE stream."""
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        plugin.client = Mock(queue=Mock())

        stats = plugin._stream_response_body(mock_response)

        assert stats.duration_ms is not None
        assert stats.duration_ms >= 0.0

    def test_sse_stream_interrupted_on_broken_pipe(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """BrokenPipeError during SSE sets completed=False, end_time is set."""
        lines = ["data: tok1", "", "data: tok2", ""]
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream"},
            lines=lines,
        )
        call_count = 0

        def queue_with_error(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise BrokenPipeError()

        plugin.client = Mock(queue=queue_with_error)

        stats = plugin._stream_response_body(mock_response)  # must not raise

        assert stats.completed is False
        assert stats.end_time is not None
        assert stats.bytes_sent > 0
```

- [ ] **Step 24: Run new SSE tests to confirm they fail**

```bash
pytest tests/test_web_server_plugin.py::TestSseStreamStats -v
```

Expected: `AttributeError: 'FlowProxyWebServerPlugin' object has no attribute '_stream_sse'`

### Task 7: Implement `_stream_sse`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` — add `_stream_sse` method

- [ ] **Step 25: Add `_stream_sse` method**

Add this method after `_stream_bytes` (before `_log_stream_stats`):

```python
def _stream_sse(self, response: httpx.Response, stats: StreamStats) -> None:
    """Stream SSE response body to client, updating stats in place.

    Uses iter_lines() (yields str) to iterate line-by-line.
    Empty strings mark SSE event boundaries (\\n\\n separators).
    Each non-empty line is encoded and queued as '<line>\\n'.
    Each empty-string boundary is queued as b'\\n' and increments event_count.
    chunks_sent counts non-empty lines only.
    Sets stats.completed = True only when the iterator exhausts cleanly.
    """
    for line in response.iter_lines():
        if line == "":
            # SSE event boundary separator
            data = b"\n"
        else:
            if stats.first_chunk_time is None:
                stats.first_chunk_time = time.perf_counter()
                self.logger.info(
                    "Received first SSE line from backend: %d chars", len(line)
                )
            data = (line + "\n").encode()
        try:
            self.client.queue(memoryview(data))
            stats.bytes_sent += len(data)
            if line == "":
                stats.event_count += 1  # count only after successful delivery
            else:
                stats.chunks_sent += 1
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if isinstance(e, OSError) and e.errno != 32:
                self.logger.warning("OS error during SSE streaming: %s", e)
            else:
                self.logger.debug(
                    "Client disconnected during SSE streaming - sent %d bytes",
                    stats.bytes_sent,
                )
            return  # completed stays False
    stats.completed = True  # loop exhausted cleanly
```

- [ ] **Step 26: Run SSE tests to confirm they pass**

```bash
pytest tests/test_web_server_plugin.py::TestSseStreamStats -v
```

Expected: 6 new tests PASS.

- [ ] **Step 27: Run full test suite**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: all tests PASS.

- [ ] **Step 28: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "feat: add _stream_sse with iter_lines() and SSE event counting"
```

---

## Chunk 4: Cleanup — Remove `_is_client_connected`

### Task 8: Delete `TestConnectionStateCheck` and clean up fixtures

**Files:**
- Modify: `tests/test_web_server_plugin.py`

- [ ] **Step 29: Delete `TestConnectionStateCheck`**

Remove the entire `TestConnectionStateCheck` class (currently around lines 178–211) — all four tests inside it:
- `test_is_client_connected_true`
- `test_is_client_connected_false_no_connection`
- `test_is_client_connected_false_no_attribute`
- `test_is_client_connected_exception_handling`

- [ ] **Step 30: Clean up `mock_plugin_args` fixture**

In `mock_plugin_args` (around line 17), remove `client.connection = Mock()` — this attribute was only needed for `_is_client_connected()` checks. Also remove the now-unused `# Mock connection for _is_client_connected` comment.

The updated fixture:

```python
@pytest.fixture
def mock_plugin_args() -> dict[str, Any]:
    """Create mock arguments for plugin initialization."""
    flags = Mock()
    flags.ca_cert_dir = None
    flags.ca_signing_key_file = None
    flags.ca_cert_file = None
    flags.ca_key_file = None
    flags.log_level = "INFO"

    client = Mock()
    event_queue = Mock()

    return {
        "uid": "test-uid",
        "flags": flags,
        "client": client,
        "event_queue": event_queue,
    }
```

- [ ] **Step 31: Run tests to confirm they still pass after test deletions**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: all remaining tests PASS.

### Task 9: Remove `_is_client_connected` from implementation

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

- [ ] **Step 32: Delete `_is_client_connected` method**

Remove the entire `_is_client_connected` method (currently around lines 368–385):

```python
def _is_client_connected(self) -> bool:
    ...
```

- [ ] **Step 33: Run full test suite to confirm clean**

```bash
pytest tests/test_web_server_plugin.py -v
```

Expected: all tests PASS.

- [ ] **Step 34: Run linters**

```bash
make lint
```

Expected: PASS (no type errors, no lint issues).

- [ ] **Step 35: Run full project tests**

```bash
make test
```

Expected: all tests PASS.

- [ ] **Step 36: Commit**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
git commit -m "refactor: remove _is_client_connected, delete TestConnectionStateCheck"
```

---

## Summary of Commits

1. `feat: add StreamStats dataclass for SSE streaming metrics`
2. `test: extend make_mock_httpx_response with lines param for SSE tests`
3. `refactor: extract _stream_bytes and dispatcher _stream_response_body with StreamStats`
4. `feat: add _stream_sse with iter_lines() and SSE event counting`
5. `refactor: remove _is_client_connected, delete TestConnectionStateCheck`
