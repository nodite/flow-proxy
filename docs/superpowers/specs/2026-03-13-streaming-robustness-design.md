# Streaming Robustness Design

**Date:** 2026-03-13
**Status:** Approved
**Component:** Flow Proxy Plugin (B3 async streaming path)

---

## 1. Goals, Non-Goals, and Scope

### 1.1 Goals

- **Prevent client-visible failures**
  Avoid Connection error, InvalidHTTPResponse, and connection closure before first byte when using B3 async streaming (e.g. Claude Code via proxy).

- **Define failure behaviour**
  Clarify how streaming requests fail: failover only (no per-request retry), error paths, and how the client is notified (503, connection close).

- **Observability**
  Unify streaming-related log semantics, define metric semantics and recommended fields, and define an extensible way to expose them; leave concrete Prometheus (or other) integration to Phase 2.

### 1.2 Non-Goals (Out of Scope for This Design)

- Per-request retry (trying another config for the same streaming request).
- Specifying a concrete monitoring stack (e.g. Prometheus) implementation in this document; only semantics and extension points.
- Changing the proxy.py framework or adding new proxy modes.

### 1.3 Scope

- Applies only to the **B3 async streaming** path: `FlowProxyWebServerPlugin` (worker + pipe + `read_from_descriptors`).
- Does not change the forward proxy plugin or non-streaming behaviour, except where configuration or behaviour is explicitly shared (e.g. client timeout).

### 1.4 Phasing

- **Phase 1**
  Client correctness (timeouts, response headers/encoding), failure-handling documentation, logs and metric semantics + extension point. Includes already-shipped fixes (client timeout, stripping Transfer-Encoding, timeout validation).
- **Phase 2**
  Optional pre-first-byte retry, metrics export (e.g. Prometheus), structured streaming logs; reserved in this design, implementation timeline not fixed.

### 1.5 Backward Compatibility

- New or touched configuration must have safe defaults and remain backward compatible; existing env vars keep current behaviour and are formalised in this spec.

---

## 2. Client-Visible Correctness

### 2.1 Connection Idle Timeout (Client Inactivity Timeout)

- **Problem**
  proxy.py uses `--timeout` for connection inactivity. For streaming, no data is sent until the backend’s first byte; if this timeout is too short, the proxy closes the connection before any response, causing Connection error / InvalidHTTPResponse.

- **Contract**
  - flow-proxy passes `--timeout` to proxy.py (seconds).
  - Source: CLI `--client-timeout`, default 600; env `FLOW_PROXY_CLIENT_TIMEOUT` (seconds), default 600.
  - Valid range: 1–86400 seconds; values outside are clamped and a warning is logged.
  - Startup log must show the clamped integer seconds; current format: `  Client timeout: {value}s` (emitted by `cli.py` startup block, `logging.INFO`).

- **Principle**
  Client timeout must be ≥ typical backend TTFB (e.g. LLM first-token latency) so the connection is not closed before the first byte.

### 2.2 Response Headers and Encoding (Avoid InvalidHTTPResponse)

- **Problem**
  If the response includes `Transfer-Encoding: chunked` but the body is sent as raw bytes (e.g. SSE lines), the client will parse as chunked and fail → InvalidHTTPResponse.

- **Contract**
  - When sending a streaming response to the client, **always strip**:
    - `connection`
    - `transfer-encoding`
  - Rationale: the body is sent as a raw byte stream; we do not implement chunked framing. Keeping transfer-encoding would make the client expect chunked format and fail.
  - For SSE, we may still add: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

- **Implementation**
  In `_send_response_headers_from()` (or equivalent main-thread header logic), apply the above stripping when building headers for the client.

- **Note on predecessor spec**
  `docs/superpowers/specs/2026-03-13-async-streaming-design.md` §`_send_response_headers_from` stated that SSE responses retain `transfer-encoding: chunked`. **This spec supersedes that clause.** The B3 path sends all responses (including SSE) as raw bytes without chunked framing, so `transfer-encoding` is unconditionally stripped for all response types. The predecessor spec's SSE exception was incorrect and has been corrected in that document.

### 2.3 Connection Lifecycle

- Before the worker enqueues `_ResponseHeaders`, the only way the client gets data is after the backend responds. If the proxy closes the connection due to client timeout before that, the client sees a closed connection with no valid HTTP response. Mitigation: set client timeout high enough (2.1).
- Client disconnect: handled via `on_client_connection_close` → `PluginPool.release()` → `_reset_request_state()` → `cancel.set()`; worker exits and resources are released. The indirection through `PluginPool` is relevant: `_reset_request_state()` is called by the pool, not directly by the plugin's `on_client_connection_close`. No extra contract.

### 2.4 Relation to Current Implementation

- Already implemented: client timeout configuration and passing to proxy, 1–86400 clamp and warning, stripping of connection and transfer-encoding in response headers.
- This section formalises the contract for future maintenance and debugging.

---

## 3. Failure Handling (Failover Only, No Retry)

### 3.1 Policy

- **Failover only, no retry**
  Once a streaming request has started (worker running), do not retry the same request with another config or backend. On failure: record error, mark current config (and http client if applicable) unhealthy, and return an error or close the connection to the client.

- **Failover scope**
  LoadBalancer’s “mark failed / next config” applies only to **later** requests; the current request is not retried with another config.

### 3.2 Error Paths and Client Response

| Stage | Condition | Behaviour |
|-------|-----------|-----------|
| Before stream | `_get_config_and_token()` throws | Log error, `_send_error(503 Service Unavailable, "Auth error")`, `clear_request_context()`, return. |
| Stream setup | Exception in `StreamingState` / pipe / `thread.start()` | Close any opened pipe fds, `_streaming_state = None`, `_send_error(500 Internal Server Error, "Failed to start streaming")`, `clear_request_context()`, return. |
| Worker | `httpx.TransportError` | Worker: log error, `mark_http_client_dirty()`, `state.error = e`, `queue.put(None)`. Main thread on sentinel: if **headers not sent** → `_send_error(503 Service Unavailable, "Upstream error")`; if **headers sent** → do not send another response body, only log warning; then `_finish_stream()` (which calls `clear_request_context()`). |
| Worker | Other exception | Worker: log error + exc_info, `state.error = e`, `queue.put(None)`. Main thread: same as above (depending on `headers_sent`). |
| Worker (pipe write failure after `_ResponseHeaders`) | `os.write(pipe_w)` raises `OSError` after headers enqueued | Worker `return`s immediately; Python `finally` block still runs — `queue.put(None)` and a best-effort `os.write(pipe_w)` are attempted. The sentinel is enqueued. If the pipe is already closed, the notification write fails silently; the main thread is not woken for that sentinel, but `_reset_request_state()` (triggered by client disconnect) closes the fds and calls `clear_request_context()`. No sentinel processing by main thread in this path; resources are cleaned up via disconnect path. |
| Client disconnect | `on_client_connection_close` → `PluginPool.release()` → `_reset_request_state()` | `set_request_context()` + log; `state.cancel.set()`; worker exits on next check; `join(timeout=2.0)`; close pipe fds; `_streaming_state = None`; `clear_request_context()`. No further writes to client. |

**Cleanup invariant:** `clear_request_context()` **must be called on every exit path** so the thread-local context does not leak into the next request reusing the same pool instance. `_finish_stream()` is the canonical call site for the normal and worker-error paths. `_reset_request_state()` covers the client-disconnect path. Auth-failure and stream-setup-failure paths call it directly before returning.

### 3.3 Interaction with LoadBalancer / ProcessServices

- **Pre-stream failover (auth phase, permitted):** `_get_config_and_token()` implements single-step failover: if JWT generation fails for one config, that config is marked failed and the next is tried. This happens before the worker starts and is acceptable.
- **Intra-stream failover (prohibited):** Once the worker thread is running, the streaming path does not switch to another config or retry the same request. On worker failure, the current request ends with 503 or connection close; the failed config/client is marked for subsequent requests only.
- On `TransportError`, call `ProcessServices.get().mark_http_client_dirty()` so subsequent requests use a new http client; the current request still ends with 503 or connection close.

### 3.4 Configuration and Phase 2

- Phase 1 adds no retry-related configuration; behaviour is as in the table and current code.
- Phase 2 “pre-first-byte retry” (if implemented) is documented as an extension or separate subsection; not part of this contract.

---

## 4. Observability

### 4.1 Streaming Log Events

- **Contract**
  All streaming-path logs use `set_request_context(req_id, "WS")` so `req_id` is present for correlation.

- **Events and suggested format** (level and semantics; exact wording may vary):

| Event | Thread | Level | Suggested content |
|-------|--------|-------|--------------------|
| Request start | main | INFO | `→ {method} {path}` |
| Auth/config | main | INFO | Existing `[LB] Using config '...'` etc. |
| Forward | main | INFO | `[FWD] Sending request to backend: {url}` |
| Backend response headers | worker | INFO | `Backend response: {status} {reason}, Transfer-Encoding: ..., Content-Length: ...` |
| First byte/line | worker | INFO | `Received first SSE line from backend: {n} chars` or `Received first chunk from backend: {n} bytes` |
| Stream success | main | INFO/WARNING | `← {status_code} [{config_name}]` (WARNING for 4xx/5xx) |
| Worker error | worker | ERROR | `Transport error — marking httpx client dirty: {e}` or `Worker error: {e}` (with exc_info) |
| Stream error (headers sent) | main | WARNING | `Stream ended with error: {e}` |
| Stream error (headers not sent) | main | ERROR | Same message; 503 already sent. |
| Stream setup failure | main | ERROR | Existing 500 path logs. |

- **Principle**
  Prefer consistent semantics and fields (req_id, config_name, status, error when relevant) over adding many new log lines; support grep and future parsing.

### 4.2 Metric Semantics and Recommended Fields

- **Contract**
  Define semantics and recommended names only; no mandate on storage or protocol. Implementations may use in-memory counters or expose via an extension point.

- **Recommended metrics** (streaming requests only):

| Name (suggestion) | Type | Semantics |
|-------------------|------|-----------|
| `stream_requests_total` | counter | Total streaming requests started (worker started). |
| `stream_responses_total` | counter | Streaming requests that received at least one chunk from backend (optional labels: status_class 2xx/4xx/5xx). |
| `stream_errors_total` | counter | Streaming requests that ended in error (optional label: reason, e.g. transport_error, worker_error, client_disconnect, startup_failed). |
| `stream_ttfb_seconds` | histogram (optional) | Time from request start to first byte from backend; buckets e.g. 1, 5, 10, 30, 60, 120. |
| `stream_duration_seconds` | histogram (optional) | Time from request start to stream end (sentinel); buckets as above or coarser. |

- **Labels**
  Optional: `config_name` / `config`, `status_class`, `error_reason`; can be added in phases.

### 4.3 Extension Point (How to Expose Metrics)

- **Phase 1**
  Reserve the extension point with a commented stub in `_finish_stream()`. Minimum Phase 1 deliverable:
  ```python
  # metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)
  ```
  This ensures Phase 2 knows exactly where to hook in without adding runtime overhead. Optionally replace with a no-op callable if wiring is preferred over a comment. Full options:
  - A **callback/hook** invoked from `_finish_stream()`, e.g. `on_stream_finished(req_id, config_name, status_code, error, ttfb_sec, duration_sec)`, so a plugin or upper layer can update in-process counters;
  - Or a **read-only interface** such as `get_streaming_stats() -> dict` for use by the same process or a future `/metrics` endpoint.
- **Not specified**
  Protocol (e.g. Prometheus text format), exposure path (e.g. `/metrics`), or aggregation/storage; those belong to Phase 2 or a separate design.
- **Principle**
  Core streaming logic must not depend on a specific monitoring implementation; it only emits events or updates in-process stats and exposes them via the extension point.

### 4.4 Phase 2 Reserved

- Optional: Prometheus exporter (e.g. `/metrics`) or push to another system using 4.2/4.3.
- Optional: Counters for “pre-first-byte retry” (e.g. `stream_retries_total`) if that is implemented later.

---

## 5. Implementation Priority and Existing Fixes

### 5.1 Fixes Already Shipped (Mapped to This Design)

- **Client idle timeout**
  `--client-timeout` / `FLOW_PROXY_CLIENT_TIMEOUT`, default 600s, passed to proxy.py `--timeout`; clamped to 1–86400s with warning; startup log shows `  Client timeout: {n}s`.
- **Response headers**
  In `_send_response_headers_from()`, always strip `connection` and `transfer-encoding` to avoid InvalidHTTPResponse when sending raw bytes. Applies unconditionally to all response types including SSE.
- **Stream setup failure**
  On pipe or `thread.start()` failure, close opened fds, clear `_streaming_state`, send `500 Internal Server Error`, call `clear_request_context()`, and return.
- **503/500 standard reason phrases**
  `_send_error()` uses `_REASON_PHRASES` dict: `500 → “Internal Server Error”`, `503 → “Service Unavailable”` (RFC 7231). Replaces previous `”Error”` placeholder.
- **`clear_request_context()` on all exit paths**
  All streaming exit paths (auth failure, stream setup failure, `_finish_stream()`, `_reset_request_state()`) call `clear_request_context()` to prevent thread-local context leaks across requests in the pool.

These are considered the implemented part of Phase 1 “client correctness” and “failure handling”; the spec lists them as done.

### 5.2 Implementation Order (Phase 1 Remainder)

1. **Documentation**
   Write this design to `docs/superpowers/specs/YYYY-MM-DD-streaming-robustness-design.md`. In the doc, list 5.1 as implemented and Sections 2–3 as the contract (timeouts, headers, error paths, failover).

2. **Standard error reason phrases** *(code change)*
   In `_send_error()`, replace the `"Error"` placeholder with RFC 7231 reason phrases: `500 Internal Server Error`, `503 Service Unavailable`. See §6.3 "Must (Phase 1)".

3. **Cleanup invariant** *(code change)*
   Ensure `clear_request_context()` is called on all streaming exit paths (auth failure, stream setup failure, `_finish_stream()`, `_reset_request_state()`). See §3.2 cleanup invariant.

4. **Logging**
   Align existing streaming logs with Section 4.1 (add or unify only where needed; ensure req_id, config_name, status/error). No requirement for many new events.

5. **Metrics semantics and extension point**
   In code or a short design paragraph, define the pluggable callback or read-only stats interface (4.2, 4.3). Add a commented stub in `_finish_stream()` so Phase 2 can hook in without a search. Do not implement Prometheus or other export in Phase 1.

6. **Phase 2**
   Optional pre-first-byte retry, metrics export (e.g. `/metrics`), TTFB/duration histograms; kept in a “Phase 2” subsection with no fixed timeline.

### 5.3 Acceptance

- Clients (e.g. Claude Code) using the proxy for streaming no longer see Connection error or InvalidHTTPResponse under reasonable client timeout and backend TTFB.
- Existing streaming unit and integration tests remain green; any new logs or extension points are covered by tests or commented where appropriate.

---

## 6. Overall Assessment: Best Practices and Breaking Changes

This section evaluates the design assuming **breaking changes are allowed** where they align with best practices.

### 6.1 Summary of Current Design

- **Decided**
  Client idle timeout configurable and clamped; response headers strip transfer-encoding and connection; failover only (no per-request retry); error paths and 503/500 behaviour specified; observability = logs + metric semantics + extension point.
- **Implemented**
  Timeout configuration and passing, header stripping, stream-setup failure cleanup, and error paths/logging largely match Sections 2–3.

### 6.2 Improvements If Breaking Changes Are Allowed

| Item | Current | Best practice / breaking change | Recommendation |
|------|---------|----------------------------------|-----------------|
| **503/500 reason phrase** | ~~`HTTP/1.1 503 Error`~~ → **implemented** `503 Service Unavailable` / `500 Internal Server Error` | Use standard reasons (RFC 7231). | **Done in Phase 1**: `_send_error()` uses `_REASON_PHRASES` dict. |
| **Error response body** | `{"error": "<message>"}`; message not escaped | Fixed schema: e.g. `{"error":"...", "code":"UPSTREAM_ERROR"}`, JSON-escape message, `Content-Type: application/json`. | **Specify in this design; implement in Phase 1 or 2**: consistent body helps clients; breaking if current clients rely on current shape; document “recommended format” and migration. |
| **Config naming** | `FLOW_PROXY_CLIENT_TIMEOUT` | Clearer: `FLOW_PROXY_IDLE_TIMEOUT` or `FLOW_PROXY_CONNECTION_TIMEOUT`. | **Optional, breaking**: in spec, document “recommended name” and deprecate old name over a transition period; or keep current name and document semantics. |
| **Timeout layering** | httpx fixed connect=30, read=600; only client idle is configurable | Document three layers: client idle, httpx connect, httpx read; make latter two configurable (env/CLI) and document relation to backend TTFB. | **Document in spec**: describe three layers and recommended ranges; implementation of connect/read configuration can be Phase 2 (changing defaults is breaking). |
| **Structured logs** | Human-readable text | JSON for key streaming events (req_id, config, status, duration, error). | **Phase 2 or optional**: better for parsing but breaks existing grep/scripts; spec can define “optional: structured streaming events” and fields. |
| **Worker join timeout** | Fixed `join(timeout=2.0)` | Configurable or derived from a single “streaming timeout” config. | **Low priority**: document current 2s; consider if a unified “streaming timeouts” strategy is added later. |

### 6.3 Recommended Additions to This Spec

- **Must (Phase 1)**
  - Use **503 Service Unavailable** as the reason phrase for 503 responses (document in Section 3 and implementation).
- **Recommended**
  - Add a **“Error response body”** subsection: recommended format (`error`, `code`, escaping, `Content-Type`), and note compatibility/migration if the current shape is kept for a while.
  - Add a **“Timeout layering”** subsection: client idle, httpx connect, httpx read; semantics, recommended ranges, and relation to TTFB; document that connect/read may become configurable later with current values as default.
- **Optional (in spec as “Phase 2 / optional”)**
  - Config rename (IDLE_TIMEOUT) and deprecation of the old name.
  - Structured streaming log format and example.
  - Worker join timeout and unified “streaming timeouts” strategy.

### 6.4 What Not to Change (Rationale)

- **No per-request retry in streaming**
  Keeps semantics and implementation simple; aligns with approved “failover only” policy.
- **No real chunked encoding**
  “Strip transfer-encoding + raw byte stream” already fixes InvalidHTTPResponse; adding real chunked framing has limited benefit and higher risk.
- **No mandatory JSON logs**
  Keep human-readable as default; structured logs as optional or Phase 2 to avoid breaking existing log pipelines.

### 6.5 Error Response Body (Recommended Format)

- **Recommended schema** (for Phase 1 or 2):
  - `error` (string): human-readable message, JSON-escaped.
  - `code` (string, optional): stable code, e.g. `AUTH_ERROR`, `UPSTREAM_ERROR`, `STREAM_START_FAILED`.
  - `Content-Type: application/json`.
- **503**
  Reason phrase: **Service Unavailable** (not “Error”). Body can use `code: "UPSTREAM_ERROR"` or `"AUTH_ERROR"` as appropriate.
- **500**
  Reason phrase: **Internal Server Error**. Body can use `code: "STREAM_START_FAILED"` or similar.
- If existing clients rely on the current `{"error": "..."}` shape, keep it during a transition period and add `code` as an optional field; document in changelog or migration note.

### 6.6 Timeout Layering

- **Three layers**
  1. **Client idle timeout** (proxy.py `--timeout`): no data sent to client for this long → connection closed. Configurable (Section 2.1); must be ≥ backend TTFB.
  2. **httpx connect timeout**: limit for establishing connection to backend (currently 30s).
  3. **httpx read timeout**: limit for time between successive reads from backend (currently 600s for streaming).
- **Recommendation**
  Document in this spec; keep connect/read as implementation defaults. If they are made configurable later, use current values (30, 600) as defaults to avoid silent behaviour change.
- **Worker join**
  Currently 2.0s when cleaning up on client disconnect; document as-is; optional to make configurable or derive from a single “streaming cleanup timeout” in Phase 2.
- **Duplication risk**
  `_streaming_worker` creates `httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)` inline (per-request override), independently of `ProcessServices.http_client`'s default timeout. The two are kept in sync only by convention. If timeout values need changing, both must be updated. Phase 2 should consider centralising these in `ProcessServices` (e.g. a `streaming_timeout` attribute) to eliminate drift.

---

## References

- B3 architecture: `docs/superpowers/specs/2026-03-13-async-streaming-design.md`
- proxy.py: client inactivity via `--timeout` in handler `is_inactive()`
- RFC 7231: 503 Service Unavailable, reason phrase
