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

`stream=None` is emitted when the request body is absent, non-JSON, or lacks the `stream` field.

### Load Balancer Line

Shortened to key=value format, config name in brackets for visual consistency:

```
[87ac66][LB] config=flow-proxy-cit seq=1
```

### JWT Line

**降为 DEBUG 级别.** The clientId is sensitive and adds noise at INFO in production. No change to content — only the log level changes.

### Forward Line (`FWD`)

Unchanged format, kept at INFO:

```
[87ac66][FWD] → https://flow.ciandt.com/flow-llm-proxy/v1/messages?beta=true
```

### Backend Response Line

The current two-line pattern:
```
Backend response: 200 OK, Transfer-Encoding: chunked, Content-Length: none
Received first SSE line from backend: 20 chars
```

Is replaced by a single line that includes TTFB (time from request send to first byte received):

```
[87ac66][WS] backend=200 transfer=chunked ttfb=6.3s
[a5d612][WS] backend=504 transfer=none    ttfb=181.0s
```

`ttfb` is measured from `state.start_time` to the moment the first non-empty chunk or SSE line is received in `_streaming_worker`. It is written to `state.ttfb` before the sentinel is enqueued (GIL ensures visibility to the main thread, same pattern as `state.error`).

### Completion Line (`←`)

Emitted by `_finish_stream()` (normal/error path) or `_reset_request_state()` (client disconnect). Always the last log line for a request.

```
[87ac66][WS] ← 200 [flow-proxy-cit] stream=True  ttfb=6.3s  duration=186.2s bytes=12480 end=ok
[a5d612][WS] ← 504 [flow-proxy-apac] stream=False ttfb=181.0s duration=181.0s bytes=132   end=ok
[707150][WS] ← 200 [flow-proxy-cit] stream=True  ttfb=6.1s  duration=187.4s bytes=8192   end=transport_error
[xxx][WS]    ← --- [flow-proxy-cit] stream=True  ttfb=6.3s  duration=12.1s  bytes=4096   end=client_disconnected
```

Status is `---` when the connection was dropped before any backend response was received (client disconnected during auth or before headers were sent).

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

### Deletions

| Item | Action |
|------|--------|
| `_log_stream_mode()` method | Deleted; logic inlined into `handle_request()` |
| `Received first SSE line from backend: N chars` | Deleted; TTFB captured in `state.ttfb` instead |
| `Received first chunk from backend: N bytes` | Deleted; same as above |
| `[JWT] Generated JWT token for <clientId>` | Level changed to DEBUG |

### Per-method responsibilities

**`handle_request()`**
- Record `start_time = time.time()`
- Parse `stream` from request body (inline, replaces `_log_stream_mode`)
- Emit `→ METHOD path stream=X` at INFO
- Pass `start_time` and `stream` into `StreamingState`

**`_streaming_worker()`**
- On first non-empty chunk/line: `state.ttfb = time.time() - state.start_time`
- Emit single `backend=STATUS transfer=X ttfb=Xs` line (replacing two-line pattern)
- Remove `first_logged` flag and associated log calls

**`read_from_descriptors()`**
- On `bytes` item: `state.bytes_sent += len(item)` before `self.client.queue()`

**`_finish_stream()`**
- Compute `duration = time.time() - state.start_time`
- Determine `end` from `state.error` type
- Emit single structured `← STATUS [config] ...` line
- Remove separate `Stream ended with error: ...` WARNING (folded into `end=` field)

**`_reset_request_state()`**
- Compute `duration = time.time() - state.start_time`
- Emit `← --- [config] stream=X ttfb=Xs duration=Xs bytes=N end=client_disconnected` at INFO
- Remove current freeform `Stream canceled (client disconnect)...` line

### proxy_plugin.py (secondary)

Two targeted changes only:

1. Entry log line gains `[config]` bracket: `→ POST /path [flow-proxy-cit]`
2. JWT log line level changed to DEBUG (consistent with web server plugin)

`on_upstream_connection_close` log line (`Upstream connection closed`) is kept unchanged — forward proxy mode lacks duration/bytes tracking and no change is needed there.

## Time Format

`ttfb` and `duration` use one decimal place (`%.1f`s). Example: `ttfb=6.3s duration=186.2s`. This keeps lines readable without excessive precision.

## Non-Goals

- No changes to the log *format* (not switching to JSON structured logging)
- No metrics pipeline or aggregation (covered by future usage-stats spec)
- No changes to `ProcessServices`, `LoadBalancer`, `JWTGenerator`, or `RequestForwarder` log lines
- No changes to `proxy_plugin.py` beyond the two items listed above
