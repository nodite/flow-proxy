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

**Level changed to DEBUG.** The clientId is sensitive and adds noise at INFO in production. No change to content — only the log level changes. The line is emitted from `JWTGenerator` (non-Goals section: no changes to `JWTGenerator` file, but the call site in `base_plugin._get_config_and_token()` does not control this level — see Code Changes).

### Forward Line (`FWD`)

Format change: prefix `Sending request to backend:` replaced with `→` for visual consistency with the entry line. Kept at INFO.

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

**Placement note:** the new backend response line replaces the current pre-loop log call (the one currently emitted when `_ResponseHeaders` is enqueued). It moves into the loop body and fires on the same iteration where `state.ttfb` is first set — before `chunk_queue.put(chunk)` on that first non-empty chunk/line. The `_ResponseHeaders` enqueue itself is retained unchanged; only the adjacent log call is relocated.

### Completion Line (`←`)

Emitted by `_finish_stream()` (normal/error path) or `_reset_request_state()` (client disconnect mid-stream). Only emitted when `self._streaming_state is not None` — if the state was never set (early-exit before streaming started), no completion line is emitted.

```
[87ac66][WS] ← 200 [flow-proxy-cit] stream=True  ttfb=6.3s  duration=186.2s bytes=12480 end=ok
[a5d612][WS] ← 504 [flow-proxy-apac] stream=False ttfb=181.0s duration=181.0s bytes=132   end=ok
[707150][WS] ← 200 [flow-proxy-cit] stream=True  ttfb=6.1s  duration=187.4s bytes=8192   end=transport_error
[xxx][WS]    ← --- [flow-proxy-cit] stream=True  ttfb=6.3s  duration=12.1s  bytes=4096   end=client_disconnected
```

Status is `---` when `_reset_request_state()` is called before any backend status code was received (i.e., `state.status_code == 0`). `ttfb` is rendered as `-` when `state.ttfb is None` (backend disconnect or client disconnect before the first byte arrived). Example: `ttfb=-`.

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

```python
@dataclass
class StreamingState:
    # existing fields unchanged ...
    start_time: float           # set in handle_request() via time.time()
    stream: bool | None         # parsed from request body; None if indeterminate
    ttfb: float | None = None   # set by worker on first chunk/line
    bytes_sent: int = 0         # incremented in read_from_descriptors() main thread only
```

`bytes_sent` is only written from the main thread (`read_from_descriptors`), so no locking is needed.

### Deletions and Level Changes

| Item | Action |
|------|--------|
| `_log_stream_mode()` method | Deleted; logic inlined into `handle_request()` |
| `Received first SSE line from backend: N chars` | Deleted; TTFB captured in `state.ttfb` instead |
| `Received first chunk from backend: N bytes` | Deleted; same as above |
| `[JWT] Generated JWT token for <clientId>` (in `JWTGenerator`) | Level changed INFO → DEBUG |
| `Stream ended with error: ...` WARNING **and** ERROR in `_finish_stream()` | Both deleted; end reason folded into `end=` field on completion line |
| `Stream canceled (client disconnect)...` INFO in `_reset_request_state()` | Deleted; replaced by structured completion line |

Note: the current code emits the "Stream ended with error" line at `WARNING` when `state.headers_sent` is True, and at `ERROR` when `state.headers_sent` is False. Both variants are deleted and replaced by the completion line's `end=` field and log level table above.

### Per-method responsibilities

**`handle_request()`**
- Record `start_time = time.time()`
- Parse `stream` from request body (inline, replaces `_log_stream_mode`; all indeterminate cases → `None`)
- Emit `→ METHOD path stream=X` at INFO
- Pass `start_time` and `stream` into `StreamingState`

**`_streaming_worker()`**
- On first non-empty chunk/line (inside `iter_bytes()` / `iter_lines()` loop, before `chunk_queue.put(chunk)`): set `state.ttfb = time.time() - state.start_time` and emit the `backend=STATUS transfer=X ttfb=Xs` line
- Remove `first_logged` flag and associated log calls

**`read_from_descriptors()`**
- On `bytes` item: `state.bytes_sent += len(item)` before `self.client.queue()`. Response header bytes (queued from `_send_response_headers_from()`) are intentionally excluded — `bytes_sent` counts payload bytes only.

**`_finish_stream()`**
- Compute `duration = time.time() - state.start_time`
- Determine `end` from `state.error` type
- Emit single structured `← STATUS [config] stream=X ttfb=Xs duration=Xs bytes=N end=X` line at level per table above
- Delete both the WARNING and ERROR `Stream ended with error: ...` log calls

**`_reset_request_state()`**
- Only emit completion line when `state is not None` (the existing `if state is None: return` guard at the top of the method already handles the None case)
- Compute `duration = time.time() - state.start_time`
- Use `state.status_code or "---"` as the status token
- Emit `← --- [config] stream=X ttfb=Xs duration=Xs bytes=N end=client_disconnected` at INFO
- Delete the current `Stream canceled (client disconnect)...` line

### FWD log line format change

In `handle_request()`, replace:
```python
self.logger.info("Sending request to backend: %s", target_url)
```
with:
```python
self.logger.info("→ %s", target_url)
```

### JWT log level change

In `JWTGenerator.generate_token()`, change:
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

1. **Reformat the existing post-auth summary log line.** The current line fires in the success path after `_get_config_and_token()` returns (after `modify_request_headers` succeeds). It stays in the same position — only the format changes. Current: `"Request processed with config '%s' → %s"`. New: `"→ %s %s [%s]"` (method, path, config_name). Example: `[xxx][PROXY] → POST /v1/messages?beta=true [flow-proxy-cit]`.
2. **JWT log level**: already handled by the `JWTGenerator` change above.

`on_upstream_connection_close` log line (`Upstream connection closed`) is kept unchanged — forward proxy mode lacks duration/bytes tracking.

## Time Format

`ttfb` and `duration` use one decimal place (`%.1f`s). Example: `ttfb=6.3s duration=186.2s`.

## Non-Goals

- No changes to the log *format* (not switching to JSON structured logging)
- No metrics pipeline or aggregation (covered by future usage-stats spec)
- No changes to `ProcessServices`, `LoadBalancer`, or `RequestForwarder` log lines
- No changes to `proxy_plugin.py` beyond the two items listed above
