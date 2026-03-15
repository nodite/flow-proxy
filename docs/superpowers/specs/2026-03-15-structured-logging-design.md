# Structured Request Logging Design

**Date:** 2026-03-15
**Status:** Draft

## Background

During investigation of recurring 504 errors and silent-failure scenarios (client receives no response despite proxy logging `← 200`), the following observability gaps were identified:

- No timing data (TTFB, total duration) in any log line
- `stream=True/False` was a separate noisy line not connected to the request summary
- End reason not distinguished: normal completion, transport error, and client disconnect all looked the same
- `bytes_sent` to client was never tracked, making it impossible to tell if data reached the client
- JWT log line emitted clientId at INFO level on every request

This design replaces the ad-hoc log lines with a structured, permanent production logging protocol.

## Scope

- **Primary:** `FlowProxyWebServerPlugin` (`web_server_plugin.py`)
- **Secondary:** `FlowProxyPlugin` (`proxy_plugin.py`) — limited alignment only

## Log Line Protocol

### Request Entry Line (`→`)

Emitted at the start of `handle_request()`. `stream=` is inlined here; the separate `_log_stream_mode()` method is deleted.

```
[87ac66][WS] → POST /v1/messages?beta=true stream=True
[a5d612][WS] → POST /v1/messages?beta=true stream=False
[xxx][WS]    → POST /v1/messages?beta=true stream=None
```

`stream=None` is emitted for all indeterminate cases: absent body, non-JSON body, or body lacking the `stream` field. The three distinct outputs previously produced by `_log_stream_mode()` (`stream=<no body>`, `stream=<parse error>`, `stream=None`) are normalized to a single `stream=None` for grepping consistency.

### Load Balancer Line

No format change. The existing `LoadBalancer` log line is unchanged (see Non-Goals). It currently emits:

```
[87ac66][LB] Using config 'flow-proxy-cit' (request #1, index 0)
```

### JWT Line

**Level changed to DEBUG.** The clientId is sensitive and adds noise at INFO in production. No change to content — only the log level changes. The line is emitted from `JWTGenerator` (see Code Changes).

### Forward Line (`FWD`)

Format change: prefix `Sending request to backend:` replaced with `→` for visual consistency with the entry line. The `with component_context("FWD"):` wrapper is retained so the `[FWD]` tag continues to appear. Kept at INFO.

Current:
```
[87ac66][FWD] Sending request to backend: https://flow.ciandt.com/flow-llm-proxy/v1/messages?beta=true
```

New:
```
[87ac66][FWD] → https://flow.ciandt.com/flow-llm-proxy/v1/messages?beta=true
```

### Backend Response Line

The current two-line pattern:
```
Backend response: 200 OK, Transfer-Encoding: chunked, Content-Length: none
Received first SSE line from backend: 20 chars
```

Is replaced by a single line that includes TTFB (time from request send to first byte received from backend):

```
[87ac66][WS] backend=200 transfer=chunked ttfb=6.3s
[a5d612][WS] backend=504 transfer=none    ttfb=181.0s
```

`ttfb` is set inline in `_streaming_worker` at the point the first non-empty chunk or SSE line is received — i.e., inside the `iter_bytes()` / `iter_lines()` loop, before `chunk_queue.put(chunk)`. This is the earliest point at which TTFB is known. The value is also written to `state.ttfb` so `_finish_stream()` can include it in the completion line; this write happens before the sentinel is enqueued, so GIL ensures visibility to the main thread when it reads `state.ttfb` after `queue.get()` returns `None` — the same pattern already used for `state.error`.

**Placement note:** the new backend response line replaces the current pre-loop log call (the one currently emitted immediately before `_ResponseHeaders` is enqueued, at the point where `response.status_code` is first available). It moves into the loop body and fires on the same iteration where `state.ttfb` is first set — before `chunk_queue.put(chunk)` on that first non-empty chunk/line. The `_ResponseHeaders` enqueue **position** is retained unchanged (it still fires before the loop); the adjacent log call is both relocated (into the loop) and reformatted (gains `ttfb=`, loses `reason_phrase` and `Content-Length`). The values `response.status_code` and `response.headers.get("transfer-encoding")` remain accessible inside the loop because the `with http_client.stream(...) as response:` block spans the entire loop.

### Completion Line (`←`)

Emitted by `_finish_stream()` (normal/error path) or `_reset_request_state()` (client disconnect mid-stream). Only emitted when `self._streaming_state is not None` — if the state was never set (early-exit before streaming started), no completion line is emitted.

```
[87ac66][WS] ← 200 [flow-proxy-cit] stream=True  ttfb=6.3s  duration=186.2s bytes=12480 end=ok
[a5d612][WS] ← 504 [flow-proxy-apac] stream=False ttfb=181.0s duration=181.0s bytes=132   end=ok
[707150][WS] ← 200 [flow-proxy-cit] stream=True  ttfb=6.1s  duration=187.4s bytes=8192   end=transport_error
[xxx][WS]    ← --- [flow-proxy-cit] stream=True  ttfb=-      duration=12.1s  bytes=0      end=client_disconnected
```

**Status token:** `state.status_code` is used when non-zero; `"---"` is used when `state.status_code == 0` (backend response not yet received). This applies in both `_finish_stream()` and `_reset_request_state()`. The format placeholder must be `%s` (not `%d`) to handle both the integer and string cases.

**`ttfb` token:** rendered as `"%.1fs" % state.ttfb` when set, or `"-"` when `state.ttfb is None` (first byte never arrived before error or disconnect).

**`end=` values:**

| Value | Condition |
|-------|-----------|
| `ok` | `state.error is None` — stream completed normally |
| `transport_error` | `isinstance(state.error, httpx.TransportError)` — gateway cut the connection |
| `worker_error` | Any other exception in `_streaming_worker` |
| `client_disconnected` | `_reset_request_state()` was called — client hung up mid-stream |

**Log level for completion line:**

| Condition | Level |
|-----------|-------|
| `status_code < 400` and `end=ok` | INFO |
| `status_code >= 400` | WARNING |
| `end=transport_error` or `end=worker_error` | WARNING |
| `end=client_disconnected` | INFO |

## Code Changes

### StreamingState New Fields

`start_time` and `stream` are required fields (no default). They are appended after the existing required field `config_name` and before the existing defaulted fields (`headers_sent`, `is_sse`, `status_code`, `error`). This preserves the `@dataclass` rule that all fields without defaults must precede all fields with defaults.

```python
@dataclass
class StreamingState:
    # existing required fields (pipe_r, pipe_w, chunk_queue, thread, cancel, req_id, config_name) unchanged
    start_time: float           # appended after config_name; set in handle_request()
    stream: bool | None         # appended after start_time; parsed from request body; None if indeterminate
    # existing defaulted fields (headers_sent, is_sse, status_code, error) unchanged
    ttfb: float | None = None   # set by worker on first chunk/line
    bytes_sent: int = 0         # incremented in read_from_descriptors() main thread only
```

`bytes_sent` is only written from the main thread (`read_from_descriptors`), so no locking is needed. `ttfb` and `bytes_sent` are appended after the existing defaulted fields.

### Deletions and Level Changes

| Item | Action |
|------|--------|
| `_log_stream_mode()` method | Deleted; logic inlined into `handle_request()` |
| `Received first SSE line from backend: N chars` | Deleted; TTFB captured in `state.ttfb` instead |
| `Received first chunk from backend: N bytes` | Deleted; same as above |
| `[JWT] Generated JWT token for <clientId>` (in `JWTGenerator`) | Level changed INFO → DEBUG |
| `Stream ended with error: ...` WARNING **and** ERROR in `_finish_stream()` | Both deleted; end reason folded into `end=` field on completion line |
| `Stream canceled (client disconnect)...` INFO in `_reset_request_state()` | Deleted; replaced by structured completion line |
| `Transport error — marking httpx client dirty: %s` ERROR in `_streaming_worker()` | Retained at WARNING; provides specific error message detail that `end=transport_error` alone does not capture |
| `Worker error: %s` ERROR in `_streaming_worker()` | Retained at WARNING; provides specific error message detail that `end=worker_error` alone does not capture |

Note: the current code emits "Stream ended with error" at `WARNING` when `state.headers_sent` is True, and at `ERROR` when `state.headers_sent` is False. Both variants are deleted and replaced by the completion line's `end=` field and log level table above. The worker-side error lines (`Transport error` and `Worker error`) are intentionally retained because they capture the specific error message; the completion line captures the category and timing. Both are at WARNING, so each error appears twice in the log: once with the message detail from the worker thread, once as the structured summary from the main thread.

### Per-method responsibilities

**`handle_request()`**
- Record `start_time = time.time()`
- Parse `stream` from request body (inline, replaces `_log_stream_mode`; all indeterminate cases → `None`)
- Emit `→ METHOD path stream=X` at INFO
- Pass `start_time` and `stream` into `StreamingState` constructor

**`_streaming_worker()`**
- On first non-empty chunk/line (inside `iter_bytes()` / `iter_lines()` loop, before `chunk_queue.put(chunk)`): set `state.ttfb = time.time() - state.start_time` and emit the `backend=STATUS transfer=X ttfb=Xs` line
- Remove `first_logged` flag and associated log calls
- Retain `"Transport error — marking httpx client dirty: %s"` and `"Worker error: %s"` lines; change both from ERROR to WARNING

**`read_from_descriptors()`**
- On `bytes` item: `state.bytes_sent += len(item)` before `self.client.queue()`. Response header bytes (queued from `_send_response_headers_from()`) are intentionally excluded — `bytes_sent` counts payload bytes only.

**`_finish_stream()`**
- Compute `duration = time.time() - state.start_time`
- Determine `end` from `state.error` type
- Render status as `str(state.status_code) if state.status_code else "---"`
- Render ttfb as `"%.1fs" % state.ttfb if state.ttfb is not None else "-"`
- Emit single structured `← %s [%s] stream=%s ttfb=%s duration=%.1fs bytes=%d end=%s` line at level per table above, using `%s` for the status token
- Delete both the WARNING and ERROR `Stream ended with error: ...` log calls

**`_reset_request_state()`**
- Only emit completion line when `state is not None` (the existing `if state is None: return` guard at the top of the method already handles the None case)
- Compute `duration = time.time() - state.start_time`
- Render status as `str(state.status_code) if state.status_code else "---"`
- Render ttfb as `"%.1fs" % state.ttfb if state.ttfb is not None else "-"`
- Emit completion line at INFO using `%s` for the status token
- Delete the current `Stream canceled (client disconnect)...` line

### FWD log line format change

In `handle_request()`, within the existing `with component_context("FWD"):` block, replace:
```python
self.logger.info("Sending request to backend: %s", target_url)
```
with:
```python
self.logger.info("→ %s", target_url)
```

### JWT log level change

In `JWTGenerator`, change:
```python
self.logger.info(f"Generated JWT token for {client_id}")
```
to:
```python
self.logger.debug("Generated JWT token for %s", client_id)
```
Note: this also fixes the f-string to `%`-style for consistency with the logging calls in `web_server_plugin.py`. Other f-string log calls in `jwt_generator.py` are out of scope for this spec.

### proxy_plugin.py (secondary)

Two targeted changes:

1. **Reformat the existing post-auth summary log line.** The current line fires in the success path after `modify_request_headers` succeeds, at the same position as today. It stays there — only the format changes. `method` is obtained via `self._decode_bytes(request.method) if request.method else "GET"` (mirroring the web server plugin pattern); `path` is obtained via `self._decode_bytes(request.path) if request.path else "unknown"` (the decoded path, not the full `target_url`). Current format: `"Request processed with config '%s' → %s"` (config_name, target_url). New format: `"→ %s %s [%s]"` (method, path, config_name). Example: `[xxx][PROXY] → POST /v1/messages?beta=true [flow-proxy-cit]`.

2. **JWT log level**: already handled by the `JWTGenerator` change above.

`on_upstream_connection_close` log line (`Upstream connection closed`) is kept unchanged — forward proxy mode lacks duration/bytes tracking.

## Time Format

`ttfb` and `duration` use one decimal place (`%.1f`s). Example: `ttfb=6.3s duration=186.2s`. When `ttfb` is unavailable, the literal string `-` is used (no `s` suffix).

## Non-Goals

- No changes to the log *format* (not switching to JSON structured logging)
- No metrics pipeline or aggregation (covered by future usage-stats spec)
- No changes to `ProcessServices`, `LoadBalancer`, or `RequestForwarder` log lines
- No changes to `proxy_plugin.py` beyond the two items listed above
