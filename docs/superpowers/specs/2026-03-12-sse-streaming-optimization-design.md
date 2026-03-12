# SSE Streaming Optimization Design

**Date:** 2026-03-12
**Status:** Approved
**Scope:** `flow_proxy_plugin/plugins/web_server_plugin.py` + tests

---

## Background

`FlowProxyWebServerPlugin._stream_response_body` currently handles both SSE and non-SSE responses via a single `iter_bytes()` loop. Issues identified:

1. **Latency**: `_is_client_connected()` is called before every chunk, logging two `logger.debug()` calls per chunk with no `isEnabledFor` guard. This adds unnecessary overhead on every iteration.
2. **No observability**: No metrics are recorded for streaming responses — TTFT, total stream duration, and event count are invisible in logs.
3. **Mixed concerns**: SSE and non-SSE responses follow the same code path despite different semantics.

The backend is LiteLLM, which flushes one SSE event per HTTP chunk (`data: {...}\n\n`), so chunks are generally SSE-event-aligned at the network layer.

---

## Goals

- Remove per-chunk overhead from `_is_client_connected()` pre-check
- Record TTFT, stream duration, and SSE event count in logs
- Split SSE and non-SSE streaming into separate, focused methods
- Maintain all existing error handling guarantees

---

## Non-Goals

- Changing httpx configuration (timeouts, connection pool)
- Modifying the forward proxy plugin (`proxy_plugin.py`)
- Adding metrics to an external system (Prometheus, etc.)

---

## Design

### 1. `StreamStats` Dataclass

Added at module level in `web_server_plugin.py` (private to the module for now).

```python
@dataclass
class StreamStats:
    start_time: float
    first_chunk_time: float | None = None
    end_time: float | None = None
    bytes_sent: int = 0
    chunks_sent: int = 0   # non-empty lines written (SSE) or non-empty byte chunks (non-SSE)
    event_count: int = 0   # SSE only: incremented on each empty line (event boundary)
    completed: bool = False  # set to True just before finally block; False means interrupted

    @property
    def ttft_ms(self) -> float | None:
        if self.first_chunk_time is None:
            return None
        return (self.first_chunk_time - self.start_time) * 1000

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None or self.first_chunk_time is None:
            return None
        return (self.end_time - self.first_chunk_time) * 1000
```

`start_time` is captured at the top of `_stream_response_body`, after the httpx stream context is already entered. `end_time` is set in a `finally` block so partial stats are always recorded even on error or disconnect.

`chunks_sent` counts non-empty lines written to the client in `_stream_sse` (blank separator lines are not counted), and non-empty byte chunks in `_stream_bytes`. This keeps the semantics consistent: it represents payload-bearing writes only.

### 2. Method Structure

`_stream_response_body` becomes a dispatcher:

```python
def _stream_response_body(self, response: httpx.Response) -> StreamStats:
    is_sse = "text/event-stream" in response.headers.get("content-type", "")
    stats = StreamStats(start_time=time.perf_counter())
    try:
        if is_sse:
            self._stream_sse(response, stats)
        else:
            self._stream_bytes(response, stats)
        stats.completed = True
    finally:
        stats.end_time = time.perf_counter()
        self._log_stream_stats(stats, is_sse)
    return stats
```

#### `_stream_sse(response, stats)`

- Uses `response.iter_lines()` which yields `str` objects (httpx decodes via `LineDecoder`)
- Empty strings (`""`) mark SSE event boundaries: `stats.event_count += 1`; the empty line is queued as `b"\n"` to preserve SSE wire format
- Non-empty lines are encoded and queued as `(line + "\n").encode()`
- `stats.chunks_sent` is incremented only for non-empty lines
- `stats.bytes_sent` is incremented for every write (including blank separator lines)
- `stats.first_chunk_time` set on first non-empty line
- Disconnect exceptions (`BrokenPipeError`, `ConnectionResetError`, `OSError`) from `client.queue()` are caught internally — method returns normally with partial stats; `stats.completed` stays `False`
- `OSError` with `errno != 32` logs a `logger.warning()` (preserving existing behavior) before returning

#### `_stream_bytes(response, stats)`

- Uses `response.iter_bytes()` (unchanged behavior for non-SSE)
- Records `stats.first_chunk_time` on first non-empty chunk
- `stats.chunks_sent` counts non-empty chunks; `stats.bytes_sent` tracks total
- Same disconnect exception handling as `_stream_sse`

#### `_log_stream_stats(stats, is_sse)`

Logs one `info`-level line after stream completes or is interrupted. Uses `stats.completed` to choose prefix:

```
# SSE complete:
SSE stream complete: TTFT=42ms, duration=3210ms, events=87, bytes=18432
# SSE interrupted (disconnect):
SSE stream interrupted: TTFT=38ms, events=12, bytes=2048
# Non-SSE complete:
Stream complete: bytes=4096, chunks=3
# Non-SSE interrupted:
Stream interrupted: bytes=1024, chunks=1
```

### 3. Removal of `_is_client_connected`

`_is_client_connected()` is removed entirely. Rationale:

- The method's only purpose is to abort streaming early on client disconnect
- `client.queue()` already raises `BrokenPipeError` / `ConnectionResetError` / `OSError` on a dead connection — these are already caught in the streaming loop
- The pre-check adds 2 `logger.debug()` calls per chunk with no `isEnabledFor` guard, making it a source of unnecessary work at high token rates

The `mock_plugin_args` fixture's `client.connection = Mock()` was set up solely for this check and can be removed.

---

## Error Handling

Disconnect exceptions (`BrokenPipeError`, `ConnectionResetError`, `OSError`) from `client.queue()` are caught **inside** `_stream_sse` / `_stream_bytes`. The methods return normally with partial stats; they do not propagate these exceptions to `handle_request`. This matches the existing behavior of `_stream_response_body`.

Other boundaries in `handle_request` remain unchanged:

- `BrokenPipeError` / `ConnectionResetError` at the outer level (outside streaming) are still caught by the existing handler
- `httpx.TransportError` continues to trigger `mark_http_client_dirty()`
- `stats.end_time` is always set (in `finally`), so partial metrics are always logged

---

## Testing

### Deleted Tests

`TestConnectionStateCheck` and all four of its tests are deleted, as `_is_client_connected` no longer exists.

### Updated Tests (`TestStreamResponseBody`)

| Test | Change |
|------|--------|
| `test_forwards_all_chunks_immediately` | Assert `stats.chunks_sent == 3`, `stats.bytes_sent == total_bytes`, `stats.completed == True` |
| `test_stops_when_client_disconnects` | Replace `connection=None` approach: `queue` raises `BrokenPipeError` on first call; assert `stats.bytes_sent == 0`, `stats.completed == False`, `stats.end_time is not None` |
| `test_handles_broken_pipe_gracefully` | Assert `StreamStats` returned (not tuple), `stats.completed == False` |
| `test_handles_connection_reset_error_gracefully` | Same |
| `test_handles_os_error_errno_32_gracefully` | Same |
| `test_skips_empty_chunks` | Assert `stats.chunks_sent == 2` |

### New Tests

- `test_sse_stream_stats_ttft` — mock `time.perf_counter` to return controlled values; assert `stats.ttft_ms` is correct
- `test_sse_stream_stats_event_count` — mock `iter_lines()` to yield SSE lines with empty-string boundaries (e.g., `["data: tok1", "", "data: tok2", ""]`); assert `stats.event_count == 2`
- `test_sse_stream_stats_duration` — assert `duration_ms` is non-None and positive after complete stream
- `test_non_sse_stream_stats` — non-SSE response via `iter_bytes()`; assert `event_count == 0`, `bytes_sent` correct, `completed == True`
- `test_stream_stats_on_broken_pipe` — queue raises after 2 non-empty lines; assert `stats.end_time is not None`, `stats.completed == False`, `stats.bytes_sent > 0`

### Test Helper Update

`make_mock_httpx_response` gains an optional `lines: list[str] | None = None` parameter that configures `response.iter_lines.return_value = iter(lines or [])` for SSE tests.

---

## Files Changed

| File | Change |
|------|--------|
| `flow_proxy_plugin/plugins/web_server_plugin.py` | Add `StreamStats`, refactor streaming methods, remove `_is_client_connected` |
| `tests/test_web_server_plugin.py` | Delete `TestConnectionStateCheck`, update existing tests, add 5 new tests, extend `make_mock_httpx_response` |

No other files require changes.
