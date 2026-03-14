# SSE Mid-Stream Error Notification

**Date:** 2026-03-14
**Status:** Approved
**Scope:** `flow_proxy_plugin/plugins/web_server_plugin.py`

## Problem

When the backend drops a chunked SSE connection mid-stream (e.g., `httpx.TransportError: incomplete chunked read`), `_finish_stream` detects `state.error` but takes no client-visible action because `state.headers_sent` is already `True`. The client's SSE stream silently ends with no indication of failure.

**Relevant log sequence:**
```
22:08:00 [f36d5e][WS] → POST /v1/messages?beta=true
22:08:07 [f36d5e][WS] Backend response: 200 OK, Transfer-Encoding: chunked
22:08:07 [f36d5e][WS] Received first SSE line from backend: 20 chars
22:11:08 [f36d5e][WS] Transport error — marking httpx client dirty: peer closed connection
          without sending complete message body (incomplete chunked read)
22:11:08 [f36d5e][WS] Stream ended with error: ...   ← WARNING, client gets nothing
```

## Design

### Changes

#### 1. Top-level `import json`

Add `import json` to the module-level imports alongside the other stdlib imports. The method `_send_sse_error_event` needs it; placing it at the top level is the correct pattern — an inline `import` inside a method body would imply a conditional dependency that does not exist.

#### 2. `StreamingState` — add `is_sse: bool = False`

`is_sse` is currently only carried by `_ResponseHeaders` and is not accessible in `_finish_stream`. Adding it to `StreamingState` makes it available at stream-end time.

The field defaults to `False`, which means existing tests that set up a `StreamingState` without `is_sse` continue to exercise the non-SSE silent-close path without modification.

`_reset_request_state` (client-disconnect path) never calls `_finish_stream` and never reads `is_sse`, so this field has no effect on early-disconnect handling.

```python
@dataclass
class StreamingState:
    ...
    is_sse: bool = False          # set by read_from_descriptors when _ResponseHeaders processed
```

#### 3. `read_from_descriptors` — propagate `is_sse` to state

When processing the `_ResponseHeaders` item, store `is_sse` on the state before marking headers as sent:

```python
if isinstance(item, _ResponseHeaders):
    state.is_sse = item.is_sse    # new line
    state.status_code = item.status_code
    self._send_response_headers_from(item)
    state.headers_sent = True
```

#### 4. `_finish_stream` — branch on `is_sse` when error after headers

The `log_func` lines remain inside the `if state.error:` block. The new `elif` branch is inserted between the 503 path and the silent-close path:

```python
if state.error:
    if not state.headers_sent:
        self._send_error(503, "Upstream error")
    elif state.is_sse:
        self._send_sse_error_event()          # new branch
    # non-SSE + headers sent: silent close (unchanged)
    log_func = self.logger.warning if state.headers_sent else self.logger.error
    log_func("Stream ended with error: %s", state.error)
else:
    log_func = (
        self.logger.info if state.status_code < 400 else self.logger.warning
    )
    log_func("← %d [%s]", state.status_code, state.config_name)
```

#### 5. New method `_send_sse_error_event()`

Injects a synthetic SSE error event compatible with the Anthropic API error format. Error message is a generic string — internal exception details are not exposed to clients.

Called only from `_finish_stream`, which is itself called only from `read_from_descriptors` (main thread). This preserves the existing invariant that `self.client.queue()` is only called from the main thread.

The exact wire format emitted:
```
event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "Upstream connection lost"}}\n\n
```

```python
def _send_sse_error_event(self) -> None:
    """Inject a synthetic SSE error event to notify the client of upstream failure.

    Called from _finish_stream (main thread only).
    Thread-safety: self.client.queue() is only called from the main thread.
    """
    payload = json.dumps({
        "type": "error",
        "error": {"type": "api_error", "message": "Upstream connection lost"},
    })
    event = f"event: error\ndata: {payload}\n\n".encode()
    self.client.queue(memoryview(event))
```

### Behaviour Matrix

| Scenario | headers not sent | headers sent + SSE | headers sent + non-SSE |
|---|---|---|---|
| **Before** | send 503 | silent | silent |
| **After** | send 503 | inject SSE error event | silent (unchanged) |

Non-SSE chunked responses have no standard mechanism for injecting semantic errors after the response body has started. Clients receiving a truncated body will raise their own parse error. Silent close remains appropriate.

## Scope

- **File:** `flow_proxy_plugin/plugins/web_server_plugin.py` only
- **Lines changed:** ~15 (one new import, one new dataclass field, one assignment, one branch, one method)
- **No new dependencies**
- **No interface changes** — `StreamingState` is internal, not part of the public API

## Testing

New test cases in `tests/test_web_server_plugin.py`:

1. **SSE stream, error after headers sent** — assert `self.client.queue` is called with
   the exact expected bytes:
   `b'event: error\ndata: {"type": "error", "error": {"type": "api_error", "message": "Upstream connection lost"}}\n\n'`

2. **Non-SSE stream, error after headers sent** — set `state.is_sse = False` explicitly,
   assert no extra bytes queued to `self.client` (silent close behaviour preserved).

3. **Any stream, error before headers sent** — assert 503 error response sent (existing
   behaviour, regression guard).

4. **`is_sse` propagation** — assert `state.is_sse` is set to `True` when the main thread
   processes a `_ResponseHeaders` item with `is_sse=True`.

**Note on existing tests:** The existing test `test_worker_error_after_headers_does_not_send_error_response` sets up `StreamingState` without `is_sse`. Because `is_sse` defaults to `False`, it will continue to exercise the non-SSE silent-close path and will pass without modification. To make the intent explicit, update that test to set `state.is_sse = False` directly.
