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
- Changing log levels for TransportError (all remain WARNING).

---

## 4. Design

### 4.1 `StreamingState` — New Field

Add `end_reason` to `StreamingState` (default `"transport_error"`):

```python
end_reason: str = "transport_error"
# Set by worker on TransportError; read by _finish_stream() for the end= log field.
```

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

All other clauses of §3.3 (failover scope, error paths, client-disconnect handling) remain in effect.

---

## 6. Testing

### 6.1 Modified Tests

Existing tests that assert `mark_http_client_dirty()` is called on `TransportError` must be updated to assert it is **not** called.

### 6.2 New Tests (`test_web_server_plugin.py`)

| Test | Verifies |
|---|---|
| `test_worker_remote_protocol_error_no_dirty` | `RemoteProtocolError` → `end_reason="remote_closed"`, `mark_http_client_dirty()` not called |
| `test_worker_connect_error_no_dirty` | `ConnectError` → `end_reason="connect_error"`, `mark_http_client_dirty()` not called |
| `test_worker_transport_error_no_dirty` | Generic `TransportError` → `end_reason="transport_error"`, `mark_http_client_dirty()` not called |
| `test_finish_stream_end_reason_remote_closed` | `_finish_stream()` logs `end=remote_closed` when `state.end_reason="remote_closed"` |
| `test_finish_stream_end_reason_connect_error` | `_finish_stream()` logs `end=connect_error` when `state.end_reason="connect_error"` |

### 6.3 Unchanged Tests

- `test_thread_safety.py`, `test_process_services.py`: `mark_http_client_dirty()` method behavior unchanged.
- All non-streaming-path tests: unaffected.

---

## 7. Files Changed

| File | Change |
|---|---|
| `flow_proxy_plugin/plugins/web_server_plugin.py` | `StreamingState`: add `end_reason` field. Worker: replace single `except httpx.TransportError` with three-layer except, remove `mark_http_client_dirty()`. `_finish_stream()`: use `state.end_reason`. |
| `tests/test_web_server_plugin.py` | Update existing TransportError tests; add 5 new tests per §6.2. |
