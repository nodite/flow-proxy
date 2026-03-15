# Transport Error Classification Design

**Date:** 2026-03-15
**Status:** Approved
**Component:** Flow Proxy Plugin — `FlowProxyWebServerPlugin` streaming worker

---

## 1. Problem

The streaming worker in `FlowProxyWebServerPlugin._streaming_worker()` catches all `httpx.TransportError` and calls `ProcessServices.mark_http_client_dirty()` unconditionally. This has two problems:

1. **Unnecessary client rebuilds.** `mark_http_client_dirty()` closes the entire `httpx.Client` (all connections) and forces a rebuild on the next request. httpx's connection pool already discards broken individual connections and self-heals — no full rebuild is needed for backend-initiated closures (e.g. `RemoteProtocolError: incomplete chunked read`) or transient connection failures.

2. **Opaque observability.** All `TransportError` subtypes emit `end=transport_error`, making it impossible to distinguish "backend actively closed the connection" from "proxy could not reach the backend" from the logs alone.

### Observed Symptom

```
WARNING [f733c1][WS] Transport error — marking httpx client dirty: peer closed connection without sending complete message body (incomplete chunked read)
WARNING [f733c1][WS] ← 200 [flow-proxy-apac] stream=True ttfb=3.9s duration=184.5s bytes=2657 end=transport_error
```

`RemoteProtocolError` (httpx subclass of `TransportError`) means the backend closed the TCP connection without sending the final `0\r\n\r\n` chunked-encoding terminator — typically a backend gateway timeout. This is a backend-side event, not a local client-state corruption.

---

## 2. Goals

- Remove `mark_http_client_dirty()` from the streaming worker; rely on httpx connection pool self-healing.
- Classify `TransportError` subtypes into distinct `end=` values for observability.
- Keep `mark_http_client_dirty()` available on `ProcessServices` for future use; do not delete it.

## 3. Non-Goals

- Retry logic for streaming requests (explicitly excluded in the streaming robustness spec).
- Changing `mark_http_client_dirty()` behavior or callers outside the streaming worker.
- Changing log levels for TransportError. All three subtypes (`RemoteProtocolError`, `ConnectError`, generic `TransportError`) retain the existing WARNING level for both the worker log line and the `_finish_stream()` completion line, consistent with the current behavior and independent of whether headers have been sent. (The robustness spec §4.1 log table distinguishes ERROR vs WARNING by `headers_sent` for some events, but the streaming-completion log line has always been WARNING regardless; this spec does not change that.)

---

## 4. Design

### 4.1 `StreamingState` — New Field

Add `end_reason` to `StreamingState` with a default of `""` (empty string). Place it after the existing defaulted fields (`headers_sent`, `is_sse`, `status_code`, `error`, `ttfb`, `bytes_sent`) to satisfy the dataclass rule that defaulted fields follow non-defaulted fields:

```python
end_reason: str = ""
# Written by worker before putting the None sentinel (same GIL write-before-sentinel
# / read-after-sentinel invariant as state.error and state.ttfb).
# Only valid when state.error is an httpx.TransportError; never read otherwise.
```

The default is `""` rather than `"transport_error"` to make it clear the field has not been set; reading it without a prior TransportError indicates a logic error. The three-layer except clauses in §4.2 always set it explicitly before putting the sentinel.

### 4.2 Streaming Worker — Three-Layer Exception Handling

Replace the single `except httpx.TransportError` block with three ordered except clauses. No call to `mark_http_client_dirty()` in any of them.

```python
except httpx.RemoteProtocolError as e:
    state.error = e
    state.end_reason = "remote_closed"
    self.logger.warning("Remote closed stream: %s", e)
except httpx.ConnectError as e:
    state.error = e
    state.end_reason = "connect_error"
    self.logger.warning("Connect error: %s", e)
except httpx.TransportError as e:
    state.error = e
    state.end_reason = "transport_error"
    self.logger.warning("Transport error: %s", e)
```

**Ordering matters:** `RemoteProtocolError` and `ConnectError` are subclasses of `TransportError`; they must be caught before the base class.

**Timeout exceptions:** `httpx.TimeoutException` and its subclasses (`ConnectTimeout`, `ReadTimeout`, `WriteTimeout`, `PoolTimeout`) are also subclasses of `TransportError` and will be caught by the generic `except httpx.TransportError` branch, producing `end=transport_error`. This is a known and intentional gap. For example, a `ReadTimeout` from the proxy's own 600s read deadline expiring is operationally different from a `RemoteProtocolError`, but both produce `end=transport_error`. Adding a fourth classification layer for timeouts is a future concern and is out of scope for this spec.

### 4.3 `_finish_stream()` — Use `end_reason`

Replace the hardcoded `end = "transport_error"` string:

```python
if isinstance(state.error, httpx.TransportError):
    end = state.end_reason   # "remote_closed" | "connect_error" | "transport_error"
else:
    end = "worker_error"
```

### 4.4 Resulting Log Values

| Scenario | `end=` value |
|---|---|
| Backend closed without final chunk (gateway timeout) | `remote_closed` |
| Proxy could not connect to backend | `connect_error` |
| Other httpx transport failure | `transport_error` |
| Non-transport worker exception | `worker_error` |
| Success | `ok` |
| Client disconnected mid-stream | `client_disconnected` |

Log format is unchanged; only the `end=` field value differs:

```
WARNING [f733c1][WS] ← 200 [flow-proxy-apac] stream=True ttfb=3.9s duration=184.5s bytes=2657 end=remote_closed
```

---

## 5. Relation to Existing Specs

This design updates the streaming worker error-handling contract originally specified in [2026-03-13-streaming-robustness-design.md](2026-03-13-streaming-robustness-design.md) §3.3:

> On `TransportError`, call `ProcessServices.get().mark_http_client_dirty()` so subsequent requests use a new http client.

**This clause is superseded.** The updated contract is: on `TransportError` in the streaming worker, do not call `mark_http_client_dirty()`; classify the error by subtype and set `state.end_reason` accordingly.

Additionally, the robustness spec §4.1 log event table row describing the worker TransportError message as `"Transport error — marking httpx client dirty: {e}"` is stale. The new worker log messages are as specified in §4.2 of this document (`"Remote closed stream: {e}"`, `"Connect error: {e}"`, `"Transport error: {e}"`).

The usage-stats part3 spec ([2026-03-15-usage-stats-part3-integration-design.md](2026-03-15-usage-stats-part3-integration-design.md) §3.4) contains a `_finish_stream()` code snippet that assumes `end = "transport_error"` is always the value for the `TransportError` branch, and a test (`test_finish_stream_records_transport_error`) written against that assumption. Both are stale after this change: the correct value passed as `error_reason` to `record_stream_event()` will be `state.end_reason` — one of `"remote_closed"`, `"connect_error"`, or `"transport_error"` — not always `"transport_error"`. The part3 spec's §3.4 snippet and test table entry must be updated accordingly when implementing that spec after this one.

All other clauses of §3.3 (failover scope, error paths, client-disconnect handling) remain in effect.

---

## 6. Testing

### 6.1 Modified Tests

The test `test_worker_transport_error_sets_state_error_and_sentinel` currently asserts that `mark_http_client_dirty()` is called on `TransportError`. It must be updated to assert it is **not** called.

### 6.2 New Tests (`test_web_server_plugin.py`)

| Test | Verifies |
|---|---|
| `test_worker_remote_protocol_error_no_dirty` | `RemoteProtocolError` → `end_reason="remote_closed"`, `mark_http_client_dirty()` not called |
| `test_worker_connect_error_no_dirty` | `ConnectError` → `end_reason="connect_error"`, `mark_http_client_dirty()` not called |
| `test_worker_transport_error_no_dirty` | Generic `TransportError` → `end_reason="transport_error"`, `mark_http_client_dirty()` not called |
| `test_finish_stream_end_reason_remote_closed` | `_finish_stream()` logs `end=remote_closed` when `state.end_reason="remote_closed"` |
| `test_finish_stream_end_reason_connect_error` | `_finish_stream()` logs `end=connect_error` when `state.end_reason="connect_error"` |
| `test_finish_stream_end_reason_transport_error` | `_finish_stream()` logs `end=transport_error` when `state.end_reason="transport_error"` (generic fallback path) |

### 6.3 Unchanged Tests

- `test_thread_safety.py`, `test_process_services.py`: `mark_http_client_dirty()` method behavior unchanged.
- All non-streaming-path tests: unaffected.

---

## 7. Files Changed

| File | Change |
|---|---|
| `flow_proxy_plugin/plugins/web_server_plugin.py` | `StreamingState`: add `end_reason` field. Worker: replace single `except httpx.TransportError` with three-layer except, remove `mark_http_client_dirty()`. `_finish_stream()`: use `state.end_reason`. |
| `tests/test_web_server_plugin.py` | Update existing TransportError tests; add 6 new tests per §6.2. |
