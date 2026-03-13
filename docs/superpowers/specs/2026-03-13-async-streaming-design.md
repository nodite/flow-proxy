# Async Streaming Design: B3 — Event-Loop-Integrated Streaming

**Date:** 2026-03-13
**Status:** Approved
**Component:** `FlowProxyWebServerPlugin`

---

## Problem

`FlowProxyWebServerPlugin.handle_request()` runs synchronously inside proxy.py's per-connection event loop thread. While it blocks on `httpx.stream()` + `iter_bytes()` / `iter_lines()`, proxy.py's `selector.select()` cycle cannot run. Since `flush()` is only called by `handle_writables()` inside the select loop, all queued response chunks accumulate in memory and are delivered to the client only after the entire upstream response has been consumed — effectively disabling streaming for the client.

**Root cause:** `handle_request()` owns the event loop thread for the full duration of the upstream response, so `flush()` is never triggered per-chunk.

**Observable symptom (confirmed in logs):** SSE requests report `first_byte=0ms` (httpx receives data immediately) but clients observe all tokens arriving at once after `duration_ms` — e.g., 11754 ms, 6723 ms. Non-SSE responses arrive as a single chunk (`chunks=1`), blocking the thread for the full network round-trip before returning.

---

## Solution: B3 — Pipe-Mediated Async Streaming

Restructure `FlowProxyWebServerPlugin` to participate in proxy.py's event model via `get_descriptors()` and `read_from_descriptors()`. `handle_request()` returns immediately after starting a worker thread; the worker feeds chunks through a `queue.Queue`, notifying the event loop via `os.pipe()`. The event loop delivers chunks to the client through its normal `flush()` path.

---

## Architecture

### Component Roles

**Worker thread** (httpx I/O, one per active request):
- Opens `httpx.stream()` context
- Extracts response headers into `_ResponseHeaders` dataclass
- Iterates `iter_bytes()` or `iter_lines()` (SSE)
- Puts all items into `chunk_queue`; writes one notification byte to `pipe_w` per item
- Puts `None` sentinel when done (or on error)
- Respects `cancel_event` to exit early on client disconnect

**Main thread** (proxy.py event loop, via hook overrides):
- `get_descriptors()` returns `[pipe_r]` while streaming is active
- `read_from_descriptors()` fires when `pipe_r` is readable: drains `chunk_queue`, calls `self.client.queue(memoryview(chunk))` for each item, sends headers on first item, tears down on sentinel
- `handle_writables()` (proxy.py native, unchanged) calls `flush()` — delivers data to client immediately

**Invariant:** `self.client` is only ever touched by the main thread.

### Data Flow

```
Worker Thread                          Main Thread (event loop)
─────────────────────────────────────  ─────────────────────────────────────
httpx.stream() opens
  → _ResponseHeaders → queue ────────▶ read_from_descriptors:
  + os.write(pipe_w, '\x00')             _send_response_headers_from(item)
  → bytes chunk → queue ──────────────▶  self.client.queue(memoryview(chunk))
  + os.write(pipe_w, '\x00')
  → bytes chunk → queue ──────────────▶  self.client.queue(memoryview(chunk))
  ...
  → None sentinel → queue ────────────▶  _finish_stream() → return True
  + os.write(pipe_w, '\x00')
                                        handle_writables (proxy.py native):
                                          flush() → bytes reach client
```

---

## Data Structures

### `StreamingState`

Holds all per-request streaming state; stored as `self._streaming_state`.

```python
@dataclass
class StreamingState:
    pipe_r: int                          # registered with selector as readable fd
    pipe_w: int                          # worker writes notification bytes here
    chunk_queue: queue.Queue             # _ResponseHeaders | bytes | None
    thread: threading.Thread
    cancel: threading.Event              # set by _reset_request_state() on disconnect
    req_id: str                          # for log context
    config_name: str                     # for final access log line
    headers_sent: bool = False           # guards error-response logic
    status_code: int = 0                 # stored after headers item is processed
    error: BaseException | None = None   # set by worker on exception
```

### `_ResponseHeaders`

Carries response metadata from worker to main thread. Contains only plain Python types — no httpx objects cross thread boundaries.

```python
@dataclass
class _ResponseHeaders:
    status_code: int
    reason_phrase: str
    headers: dict[str, str]    # extracted from httpx.Headers
    is_sse: bool               # True if Content-Type: text/event-stream
```

### Queue Protocol

Items appear in strict order:

| Position | Type | Main thread action |
|----------|------|--------------------|
| First | `_ResponseHeaders` | Call `_send_response_headers_from(item)` |
| 2…N | `bytes` | `self.client.queue(memoryview(item))` |
| Last | `None` (sentinel) | `_finish_stream(state)` → return True |

On error: worker stores exception in `state.error`, then puts the sentinel. Main thread checks `state.error` when processing the sentinel.

---

## Implementation

### `handle_request()` (new)

Responsibilities: authenticate, build request params, create `StreamingState`, start worker, return.

```python
def handle_request(self, request: HttpParser) -> None:
    req_id = secrets.token_hex(3)
    set_request_context(req_id, "WS")
    # NOTE: clear_request_context() is deferred to _finish_stream()

    method = self._decode_bytes(request.method) if request.method else "GET"
    path   = self._decode_bytes(request.path)   if request.path   else "/"
    self.logger.info("→ %s %s", method, path)

    try:
        _, config_name, jwt_token = self._get_config_and_token()
    except Exception as e:
        self.logger.error("Auth failed: %s", e)
        self._send_error(503, "Auth error")
        clear_request_context()
        return

    # build target_url, headers, body (unchanged from current impl)
    ...

    pipe_r, pipe_w = os.pipe()
    state = StreamingState(
        pipe_r=pipe_r, pipe_w=pipe_w,
        chunk_queue=queue.Queue(),
        cancel=threading.Event(),
        req_id=req_id,
        config_name=config_name,
        thread=threading.Thread(target=self._dummy),  # replaced below
    )
    self._streaming_state = state
    state.thread = threading.Thread(
        target=self._streaming_worker,
        args=(method, target_url, headers, body, state),
        name=f"streaming-{req_id}",
        daemon=True,
    )
    state.thread.start()
    # Return immediately — event loop resumes
```

### `_streaming_worker()` (new)

Runs in background thread. Owns the entire httpx response lifecycle.

```python
def _streaming_worker(
    self,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    state: StreamingState,
) -> None:
    set_request_context(state.req_id, "WS")
    try:
        http_client = ProcessServices.get().get_http_client()
        with http_client.stream(
            method=method, url=url, headers=headers, content=body,
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
        ) as response:
            is_sse = "text/event-stream" in response.headers.get("content-type", "")
            state.chunk_queue.put(_ResponseHeaders(
                status_code=response.status_code,
                reason_phrase=response.reason_phrase,
                headers=dict(response.headers),
                is_sse=is_sse,
            ))
            os.write(state.pipe_w, b'\x00')

            iterator = response.iter_lines() if is_sse else response.iter_bytes()
            for raw in iterator:
                if state.cancel.is_set():
                    break
                chunk: bytes = self._encode_sse_line(raw) if is_sse else raw
                if chunk:
                    state.chunk_queue.put(chunk)
                    os.write(state.pipe_w, b'\x00')

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
            os.write(state.pipe_w, b'\x00')
        except OSError:
            pass  # pipe may already be closed (client disconnected)
```

`_encode_sse_line(raw: str) -> bytes` replicates current `_stream_sse` encoding: empty string → `b"\n"`, otherwise `(raw + "\n").encode()`.

### `get_descriptors()` (new override)

```python
async def get_descriptors(self) -> tuple[list[int], list[int]]:
    if self._streaming_state:
        return [self._streaming_state.pipe_r], []
    return [], []
```

### `read_from_descriptors()` (new override)

```python
async def read_from_descriptors(self, readables: list[int]) -> bool:
    state = self._streaming_state
    if not state or state.pipe_r not in readables:
        return False

    os.read(state.pipe_r, 256)          # drain notification bytes (batch)
    set_request_context(state.req_id, "WS")

    while not state.chunk_queue.empty():
        item = state.chunk_queue.get_nowait()

        if isinstance(item, _ResponseHeaders):
            state.status_code = item.status_code
            self._send_response_headers_from(item)
            state.headers_sent = True

        elif item is None:              # sentinel — stream ended
            self._finish_stream(state)
            return True                 # signal proxy.py to close connection

        else:                           # bytes chunk
            self.client.queue(memoryview(item))

    return False
```

### `_finish_stream()` (new)

```python
def _finish_stream(self, state: StreamingState) -> None:
    if state.error:
        if not state.headers_sent:
            self._send_error(503, "Upstream error")
        log_func = self.logger.warning if state.headers_sent else self.logger.error
        log_func("Stream ended with error: %s", state.error)
    else:
        log_func = self.logger.info if state.status_code < 400 else self.logger.warning
        log_func("← %d ... [%s]", state.status_code, state.config_name)
    clear_request_context()
```

### `_reset_request_state()` (new override)

Called by `PluginPool.release()` when the connection closes.

```python
def _reset_request_state(self) -> None:
    state = self._streaming_state
    if state is None:
        return
    state.cancel.set()
    state.thread.join(timeout=2.0)
    for fd in (state.pipe_r, state.pipe_w):
        try:
            os.close(fd)
        except OSError:
            pass
    self._streaming_state = None
```

### `_send_response_headers_from()` (new)

Replaces `_send_response_headers(response: httpx.Response)` for the B3 path. Takes the plain-data `_ResponseHeaders` and calls `self.client.queue()` with the same logic as the current implementation.

---

## Error Handling

| Scenario | Worker action | Main thread action |
|----------|---------------|--------------------|
| `httpx.TransportError` | Mark client dirty, store in `state.error`, put sentinel | If headers not sent → send 503; log error |
| Other exception | Store in `state.error`, put sentinel | Same as above |
| Client disconnect (BrokenPipeError from flush) | proxy.py teardown → `_reset_request_state()` → `cancel.set()` | Worker detects cancel, breaks loop, puts sentinel |
| Worker already done when cancel fires | `join()` returns immediately | No-op |
| pipe_w already closed when worker writes final byte | `OSError` caught in worker's `finally` | Ignored |

**Headers-already-sent rule:** If the worker errors after the `_ResponseHeaders` item has been processed (i.e., `state.headers_sent = True`), the main thread cannot send a 4xx/5xx response (headers are committed). It logs the error and lets the connection close, which signals the error to the client through connection termination.

---

## Logging

Log lines emitted by this feature:

| Event | Thread | Level | Format |
|-------|--------|-------|--------|
| Incoming request | main | INFO | `→ POST /v1/messages` |
| JWT/LB selection | main | INFO | `[LB] Using config '...'` |
| Backend request | main | INFO | `[FWD] Sending request to backend: ...` |
| Backend response headers | worker | INFO | `Backend response: 200 OK, ...` |
| First chunk | worker | INFO | `Received first SSE line / first chunk` |
| Stream end | main | INFO | `← 200 OK [config_name]` |
| Worker error | worker | ERROR | `Worker error: ...` |

`set_request_context(req_id, "WS")` is called in both the worker thread and in `read_from_descriptors()` so all log lines carry the correct request ID prefix regardless of which thread emits them.

---

## Files Changed

| File | Change |
|------|--------|
| `flow_proxy_plugin/plugins/web_server_plugin.py` | Major refactor: add `StreamingState`, `_ResponseHeaders`, worker, hook overrides; remove blocking stream loop |
| `flow_proxy_plugin/plugins/base_plugin.py` | No change needed (`_reset_request_state()` contract already exists) |
| `tests/test_web_server_plugin.py` | Replace streaming tests; add new test cases for async path |

---

## Tests

| Test | What it verifies |
|------|-----------------|
| `test_streaming_worker_puts_headers_then_chunks_then_sentinel` | Worker puts `_ResponseHeaders`, N `bytes` chunks, `None` in correct order |
| `test_streaming_worker_sse_encodes_lines` | SSE lines encoded correctly (`"\n"` boundary, `line + "\n"` for content) |
| `test_read_from_descriptors_queues_each_chunk` | Each bytes item results in one `client.queue()` call |
| `test_read_from_descriptors_sends_headers_on_first_item` | `_send_response_headers_from` called once, before any chunk |
| `test_read_from_descriptors_returns_true_on_sentinel` | Sentinel causes `return True` (teardown) |
| `test_client_disconnect_sets_cancel_event` | `_reset_request_state()` sets `cancel`, worker exits loop |
| `test_worker_error_before_headers_sends_503` | `state.headers_sent = False` + `state.error` set → `_send_error(503)` called |
| `test_worker_error_after_headers_logs_only` | `state.headers_sent = True` + `state.error` → no error response, only log |
| `test_reset_cleans_up_pipe_fds` | After `_reset_request_state()`, both pipe fds are closed |
