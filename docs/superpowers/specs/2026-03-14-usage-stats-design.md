# Usage Statistics Design

**Date:** 2026-03-14
**Status:** Approved
**Component:** Flow Proxy Plugin — Usage Statistics (requests / tokens / costs)

---

## 1. Goals, Non-Goals, and Scope

### 1.1 Goals

- **Per-request usage tracking**
  Record `prompt_tokens`, `completion_tokens`, `total_tokens`, estimated `cost_usd`, `model`, `config_name`, and `duration_ms` for every streaming request that completes (or partially completes).

- **Non-invasive pipeline**
  The main streaming pipeline (`chunk_queue` → `read_from_descriptors` → client) must not be modified. Usage parsing runs on a fully independent path.

- **Local cost estimation**
  Compute `cost_usd` from LiteLLM's `/model/info` pricing endpoint, cached in-process with a 1-hour TTL. Failures degrade gracefully (cost recorded as `None`).

- **Structured log output**
  Emit one structured `USAGE` log line per request into the existing log system, queryable by `req_id`, `config_name`, and `model`.

### 1.2 Non-Goals

- Real-time aggregation dashboards or metrics export (Phase 2).
- Per-user or per-team budgeting.
- Non-streaming requests (forward proxy / `FlowProxyPlugin`); can be added later.
- Modifying client requests to inject `stream_options: {include_usage: true}` — this is a separate concern; the design degrades gracefully when usage is absent.

### 1.3 Scope

- Applies only to the **B3 async streaming path**: `FlowProxyWebServerPlugin`.
- Two new modules: `flow_proxy_plugin/utils/usage_parser.py`, `flow_proxy_plugin/utils/pricing_cache.py`.
- Minor additions to `StreamingState`, `ProcessServices`, and `_streaming_worker`.

---

## 2. Architecture

### 2.1 Data Flow

```
                     ┌─────────────────────────────────────────────┐
                     │  _streaming_worker (daemon thread)          │
                     │                                             │
  httpx stream ─────►│  for chunk in response (bytes only):       │
                     │    chunk_queue.put(chunk)  ← main pipeline  │
                     │    usage_queue.put(chunk)  ← new, bytes only│
                     │                                             │
                     │  NOTE: _ResponseHeaders is NOT put into     │
                     │  usage_queue; only bytes chunks are.        │
                     │                                             │
                     │  finally:                                   │
                     │    chunk_queue.put(None)   ← unchanged      │
                     │    usage_queue.put(None)   ← new sentinel   │
                     └─────────────────────────────────────────────┘
                             │                      │
                             ▼                      ▼
                  chunk_queue (unchanged)    usage_queue (new, unbounded)
                             │                      │
                             ▼                      ▼
            main thread /              UsageParser daemon thread
            read_from_descriptors      parse SSE → extract usage
            (zero changes)             query PricingCache → cost_usd
                                       write structured USAGE log line
```

**Key invariants:**
- `chunk_queue` and all main-thread logic are **not modified**.
- `bytes` objects are immutable; putting the same reference into two queues has zero copy overhead.
- Only `bytes` chunks are put into `usage_queue`. The `_ResponseHeaders` item placed first into `chunk_queue` is **not** put into `usage_queue`.
- `usage_queue` is an unbounded `queue.Queue()`. Because `UsageParser` only reads from it (no blocking HTTP calls during chunk consumption — pricing is queried only after the sentinel is received), backpressure is not a concern during the stream. Memory usage is bounded by stream size, which is acceptable for LLM response sizes.
- The `UsageParser` thread is a daemon; it exits when the process exits or when it receives the `None` sentinel.

### 2.2 Model Name Extraction (A+B combined)

1. **Primary**: `handle_request()` parses the request body JSON and reads the top-level `"model"` field. Stored as `request_model` (plain `str`) passed to `UsageParser`.
2. **Fallback**: `UsageParser` reads the `"model"` field from the first valid SSE `data:` chunk if `request_model` is empty.
3. **Final fallback**: model recorded as `"unknown"`, cost recorded as `None`. Log line is still written.

### 2.3 SSE vs Non-SSE Parsing

`UsageParser` is told whether the response is SSE via the `is_sse: bool` argument passed at construction time (extracted from `_ResponseHeaders.is_sse` by the main thread before launching the `UsageParser` thread — see §5.2).

- **SSE mode (`is_sse=True`)**: Worker feeds already-encoded SSE lines (output of `_encode_sse_line`). Each `bytes` item in `usage_queue` is one complete line (`b"data: {...}\n"` or `b"\n"` for blank separator lines). No cross-chunk buffering needed. `UsageParser` checks each item for a `data: ` prefix and attempts JSON parse. **Note:** this no-buffering guarantee holds only because the SSE path in `_streaming_worker` uses `response.iter_lines()` (one item per complete line). If that path is ever changed to `iter_bytes()`, line-buffer mode must be used regardless of `is_sse`.
- **Non-SSE mode (`is_sse=False`)**: Worker feeds raw binary chunks of arbitrary size. `UsageParser` maintains an internal line buffer, appending each chunk and splitting on `b"\n"` to extract complete lines. Applies the same `data: ` prefix scan. This handles potential usage data in non-SSE JSON streaming responses.

In both modes, `data: [DONE]` lines are skipped without JSON parse.

---

## 3. Components

### 3.1 `UsageRecord` dataclass

```python
@dataclass
class UsageRecord:
    req_id: str
    config_name: str
    model: str                      # "unknown" if not determinable
    prompt_tokens: int | None       # None if usage absent in response
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None          # None if pricing unavailable
    duration_ms: float              # monotonic: handle_request start → None sentinel received (excludes pricing lookup)
```

### 3.2 `UsageParser` (`flow_proxy_plugin/utils/usage_parser.py`)

A standalone class with a single public method:

```python
def run(
    self,
    usage_queue: queue.Queue,
    req_id: str,
    config_name: str,
    request_model: str,
    start_time: float,
    is_sse: bool,
) -> None:
```

Intended to be the target of a daemon `threading.Thread`. All arguments are plain scalars or a queue — no reference to `StreamingState` or any main-thread object is held.

**Responsibilities:**
- Call `set_request_context(req_id, "WS")` at entry.
- Call `clear_request_context()` before exit, on both normal and exception paths.
- Consume `bytes` chunks from `usage_queue` until `None` sentinel.
- For SSE mode: each item is a complete line; check for `data: ` prefix and JSON parse.
- For non-SSE mode: maintain a line buffer; split on `b"\n"` to extract complete lines.
- Skip `data: [DONE]` without JSON parse.
- From the **first** chunk containing a non-empty `"model"` field: set fallback model name (only used if `request_model` is empty).
- From the **last** chunk containing `"usage"` with non-null token counts: extract `prompt_tokens`, `completion_tokens`, `total_tokens`.
- After sentinel received: record `end_time = time.monotonic()` (before querying `PricingCache`), compute `duration_ms = (end_time - start_time) * 1000`, build `UsageRecord`, query `PricingCache`, emit log line.
- Entire body wrapped in `try/except Exception`; any failure logs `ERROR` with `exc_info=True` and calls `clear_request_context()` before returning.

**Does NOT:**
- Hold any reference to `StreamingState`, `self.client`, `chunk_queue`, pipe fds, or any other main-thread resource.
- Block the worker or main thread.
- Call `PricingCache` while consuming chunks (pricing query happens only after sentinel).

### 3.3 `PricingCache` (`flow_proxy_plugin/utils/pricing_cache.py`)

A process-level singleton (held by `ProcessServices`) providing model pricing.

```python
class PricingCache:
    def get_price(self, model: str) -> tuple[float, float] | None:
        """Returns (input_cost_per_token, output_cost_per_token) or None."""
```

**Behaviour:**
- Initialised with `target_base_url` (from `request_forwarder.target_base_url`).
- Owns a **dedicated `httpx.Client`** (separate from `ProcessServices.http_client`) with `timeout=httpx.Timeout(connect=5.0, read=10.0)`. This avoids races with `mark_http_client_dirty()` on the main thread.
- Lazy-loads pricing on first request for each model by calling `GET {target_base_url}/model/info`.
- **`/model/info` response schema:** Returns `{"data": [{model_name, litellm_params, model_info: {input_cost_per_token, output_cost_per_token, ...}}]}`. Pricing is extracted by iterating `data[]` and matching `entry["model_name"] == model` or `entry["litellm_params"]["model"] == model`. The fields are at `entry["model_info"]["input_cost_per_token"]` and `entry["model_info"]["output_cost_per_token"]`.
- Caches results per model name with a 1-hour TTL (wall clock, `time.time()`).
- Uses `threading.Lock` to protect the cache dict (double-checked pattern: check outside lock, then inside lock before fetch).
- On HTTP error, timeout, or malformed response: logs `WARNING`, returns `None`. Does not raise.
- `reset()` method closes the dedicated `httpx.Client` and clears the cache dict (for tests).

### 3.4 `StreamingState` additions

Three new fields added to the existing `StreamingState` dataclass. `usage_queue` and `start_time` are **required constructor arguments** (no default), consistent with `pipe_r`/`pipe_w`/`chunk_queue`. Only `request_model` has a default.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `usage_queue` | `queue.Queue[bytes \| None]` | — (required) | Independent channel from worker to `UsageParser`. Created per-request in `handle_request()`. |
| `start_time` | `float` | — (required) | `time.monotonic()` recorded at start of `handle_request()`. |
| `request_model` | `str` | `""` | Model name extracted from request body; empty string if absent. |
| `usage_parser_thread` | `threading.Thread \| None` | `None` | Set by `read_from_descriptors()` when `_ResponseHeaders` is first dequeued; joined by `_reset_request_state()`. |

### 3.5 Log Format

One structured `INFO` log line emitted by `UsageParser` after stream completion, using `set_request_context` (so `[req_id][WS]` prefix is present):

```
[abc123][WS] USAGE model=claude-3-5-sonnet config=prod-1 prompt_tokens=1024 completion_tokens=512 total_tokens=1536 cost_usd=0.003456 duration_ms=4231
```

Fields with `None` values are omitted from the log line (e.g. `cost_usd` omitted when pricing unavailable; token fields omitted when usage absent in response).

---

## 4. Failure Handling and Edge Cases

### 4.1 Missing `usage` in Response

When the client does not send `stream_options: {include_usage: true}` and LiteLLM is not configured with `always_include_stream_usage: true`, no usage chunk appears in the stream.

**Behaviour:** `UsageParser` logs the line with `prompt_tokens`, `completion_tokens`, `total_tokens`, and `cost_usd` omitted. No exception raised.

### 4.2 `PricingCache` Failure

HTTP error, timeout, or malformed response from `/model/info`.

**Behaviour:** `WARNING` logged (once per failure, not per request), `get_price()` returns `None`, cost omitted from `UsageRecord`. Next request for the same model will retry (no negative TTL caching of failures).

### 4.3 `UsageParser` Thread Exception

Any unhandled exception inside `run()`.

**Behaviour:** `ERROR` logged with `exc_info=True`. `clear_request_context()` called. Thread exits. No impact on main pipeline or worker.

### 4.4 Client Disconnect Before Stream Ends

`_reset_request_state()` is called, which sets `cancel` and joins the worker thread (`timeout=2.0s`). The worker's `finally` block puts `None` into both `chunk_queue` and `usage_queue`.

**Join ordering:** `_reset_request_state()` joins the **worker thread first**, then the **UsageParser thread**. This ensures the worker's `finally` block (which delivers the `usage_queue` sentinel) has completed before the `UsageParser` join begins, avoiding unnecessary timeout.

**Daemon-abandonment race:** If the worker join times out (2.0s), the worker's `finally` block may still run asynchronously. `_reset_request_state()` still attempts to join the `UsageParser` thread (`timeout=2.0s`). The `UsageParser` is designed so that logging after sentinel receipt is safe even if the pool instance has been reused, because it holds only the plain scalar values (`req_id`, `config_name`, etc.) captured at launch time — no reference to the `StreamingState` or the plugin instance. The `UsageParser` thread also holds a reference to `usage_queue` via its argument; this prevents the queue from being GC'd while the thread lives. If the abandoned worker eventually delivers `usage_queue.put(None)`, the `UsageParser` receives it and exits cleanly.

### 4.5 Stream Setup Failure

On pipe/`thread.start()` failure, `_streaming_state` is set to `None` and `clear_request_context()` is called. The `UsageParser` thread is never started (it is launched after both threads are confirmed started), so no cleanup needed.

### 4.6 `PricingCache` Target URL

`PricingCache` receives `target_base_url` at construction from `ProcessServices` (via `request_forwarder.target_base_url`). No new configuration required.

---

## 5. Integration Points

### 5.1 `ProcessServices` changes

- Add `pricing_cache: PricingCache` attribute, initialised in `_initialize()` with `target_base_url=self.request_forwarder.target_base_url`.
- Add `pricing_cache.reset()` call in `reset()` for test isolation.

### 5.2 `handle_request()` changes

1. Extract `request_model` from request body JSON (top-level `"model"` field); default to `""` on any error.
2. Record `start_time = time.monotonic()`.
3. Create `usage_queue = queue.Queue()`.
4. Add `usage_queue`, `start_time`, `request_model` to `StreamingState` construction.
5. `is_sse` is only known after the worker delivers the `_ResponseHeaders` item. Therefore the `UsageParser` daemon thread is **not** started in `handle_request()` but in `read_from_descriptors()` when the `_ResponseHeaders` item is first dequeued. At that point, `read_from_descriptors()` sets `state.usage_parser_thread` (guarded by `if state.usage_parser_thread is None:`) and calls `thread.start()` with `is_sse` from the dequeued `_ResponseHeaders`. `read_from_descriptors()` is called from the main-thread event loop and is non-reentrant; the `_ResponseHeaders` item is enqueued exactly once, so the guard is a safety measure rather than a race fix.

6. All other worker launch logic is unchanged.

### 5.3 `_streaming_worker()` changes

- For each `bytes` chunk: call `state.usage_queue.put(chunk)` **immediately after** `state.chunk_queue.put(chunk)` and **before** the `os.write(state.pipe_w, ...)` notification call. This ordering ensures that an `OSError` on `os.write` (early-return on client disconnect) does not leave a chunk in `chunk_queue` but missing from `usage_queue`.
- This applies only to `bytes` chunks — the `_ResponseHeaders` item put at the top of the worker is **not** put into `usage_queue`.
- In `finally`: after `chunk_queue.put(None)`, also call `state.usage_queue.put(None)`.

### 5.4 `_reset_request_state()` changes

1. Set `state.cancel`.
2. Join worker thread (`timeout=2.0s`). ← existing
3. Join `UsageParser` thread (`state.usage_parser_thread`, if not `None`) with `timeout=2.0s`. ← new
   - `usage_parser_thread` may be `None` if the client disconnected before `read_from_descriptors()` had dequeued the `_ResponseHeaders` item (i.e., headers never arrived). In that case the join is skipped and no `UsageParser` log line will be emitted for the request.

### 5.5 `_finish_stream()` — Phase 2 metrics hook

The existing stub comment:
```python
# metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)
```
remains unchanged. The `UsageRecord` emitted by `UsageParser` provides the data that a future hook would expose.

---

## 6. Testing

### 6.1 New: `tests/test_usage_parser.py`

| Test | Assertion |
|------|-----------|
| `test_parse_usage_from_sse_chunks` | Feed pre-encoded SSE lines including final usage chunk (`is_sse=True`); assert correct token counts extracted. |
| `test_parse_usage_non_sse` | Feed raw bytes with partial-line splits (`is_sse=False`); assert correct token counts via line buffering. |
| `test_parse_model_from_first_chunk` | `request_model=""`; first chunk has `"model"` field; assert extracted as fallback. |
| `test_request_model_takes_priority` | `request_model="req-model"`; response also has `"model": "resp-model"`; assert `"req-model"` used. |
| `test_missing_usage_graceful` | All chunks lack usage; assert `UsageRecord` has `None` tokens and no `cost_usd`, no exception. |
| `test_unknown_model_no_cost` | model `"unknown"`, pricing unavailable; assert `cost_usd=None`, log line written. |
| `test_done_sentinel_ignored` | `data: [DONE]` line does not cause JSON parse error. |
| `test_parser_exception_logged_and_context_cleared` | Inject queue that raises on `get()`; assert `ERROR` logged with `exc_info`, `clear_request_context()` called. |
| `test_set_and_clear_request_context` | Assert `set_request_context` called at entry and `clear_request_context` called at exit (both normal and exception). |

### 6.2 New: `tests/test_pricing_cache.py`

| Test | Assertion |
|------|-----------|
| `test_fetch_and_cache` | First call hits HTTP (`/model/info`); second call for same model uses cache, no HTTP. |
| `test_ttl_expiry_triggers_refetch` | Artificially expire TTL; next call re-fetches. |
| `test_failure_returns_none` | `/model/info` returns 500; `get_price()` returns `None`, logs `WARNING`. |
| `test_timeout_returns_none` | `/model/info` times out; `get_price()` returns `None`, logs `WARNING`. |
| `test_thread_safe_concurrent_access` | Multiple threads call `get_price()` concurrently; no corruption; HTTP called exactly once. |
| `test_reset_clears_cache` | `reset()` clears all cached entries; next call re-fetches. |
| `test_model_info_json_path` | Mock response with nested `data[].model_info.input_cost_per_token`; assert correct values extracted. |

### 6.3 Modified: `tests/test_web_server_plugin.py`

| Test | Change |
|------|--------|
| `test_streaming_state_defaults` | Assert `usage_queue` is a `Queue`, `start_time` is `float`, `request_model` defaults to `""`. Assert `usage_parser_thread` defaults to `None`. |
| `test_handle_request_launches_worker` | Assert `usage_queue` created and stored in `StreamingState`; `UsageParser` thread started in `read_from_descriptors()` after headers dequeued. |
| `test_worker_sentinel_delivered_to_usage_queue` | After worker completes, assert `usage_queue` contains `None` sentinel (worker `finally` delivers it). |
| `test_reset_request_state_join_order` | After `_reset_request_state()`, assert worker thread joined before `UsageParser` thread. |

### 6.4 Modified: `tests/test_process_services.py`

- Assert `pricing_cache` is initialised in `ProcessServices.get()`.
- Assert `pricing_cache.reset()` called in `ProcessServices.reset()`.

---

## 7. Backward Compatibility

- No configuration changes; no new environment variables.
- No changes to request or response wire format.
- `PricingCache` uses `target_base_url` from `RequestForwarder`; no new secrets or endpoints required.
- `usage_queue` and `start_time` are required `StreamingState` constructor arguments (no default); `request_model` defaults to `""`; `usage_parser_thread` defaults to `None`. `usage_queue` and `start_time` are constructed in `handle_request()`; `usage_parser_thread` is set in `read_from_descriptors()`.

---

## References

- Streaming robustness design: `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md`
- B3 async streaming design: `docs/superpowers/specs/2026-03-13-async-streaming-design.md`
- LiteLLM streaming usage: `stream_options: {include_usage: true}` / proxy `always_include_stream_usage: true`
- LiteLLM pricing endpoint: `GET /model/info` → `data[].model_info.{input_cost_per_token, output_cost_per_token}`; match by `data[].model_name` or `data[].litellm_params.model`
