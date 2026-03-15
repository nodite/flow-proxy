# Usage Statistics — Part 1: UsageParser + PricingCache

**Date:** 2026-03-15
**Status:** Draft
**Component:** Flow Proxy Plugin — Usage Statistics (Part 1 of 3)
**Prerequisite:** None
**Next:** Part 2 (UsageStats persistence), Part 3 (ProcessServices integration + test fixes)

---

## 1. Goals and Scope

This spec covers the **data extraction and pricing layer** of the usage statistics feature:

- Three new modules: `UsageRecord` + `UsageParser` in `flow_proxy_plugin/utils/usage_parser.py`, `PricingCache` in `flow_proxy_plugin/utils/pricing_cache.py`.
- `StreamingState` new fields: `usage_queue` (new required), `request_model` (new optional), `usage_parser_thread` (new optional).
- `_streaming_worker()` changes: feed `usage_queue` alongside `chunk_queue`.
- `read_from_descriptors()` changes: launch `UsageParser` daemon thread when `_ResponseHeaders` is first dequeued.
- New test files: `tests/test_usage_parser.py`, `tests/test_pricing_cache.py`.

**Out of scope for Part 1:**
- `UsageStats` in-memory counters and `stats.json` persistence (Part 2).
- `ProcessServices` additions and `record_stream_event()` call sites (Part 3).
- `_finish_stream()` and `_reset_request_state()` stats integration (Part 3).

---

## 2. Architecture

### 2.1 Data Flow

```
                     ┌─────────────────────────────────────────────┐
                     │  _streaming_worker (daemon thread)          │
                     │                                             │
  httpx stream ─────►│  for chunk in response (bytes only):       │
                     │    chunk_queue.put(chunk)  ← unchanged      │
                     │    usage_queue.put(chunk)  ← NEW            │
                     │                                             │
                     │  NOTE: _ResponseHeaders is NOT put into     │
                     │  usage_queue; only bytes chunks are.        │
                     │                                             │
                     │  finally:                                   │
                     │    chunk_queue.put(None)   ← unchanged      │
                     │    usage_queue.put(None)   ← NEW sentinel   │
                     └─────────────────────────────────────────────┘
                             │                      │
                             ▼                      ▼
                  chunk_queue (unchanged)    usage_queue (new, unbounded)
                             │                      │
                             ▼                      ▼
            main thread /              UsageParser daemon thread
            read_from_descriptors      parse SSE → extract usage
            (minimal change)           query PricingCache → cost_usd
                                       emit structured USAGE log line
                                       call usage_stats.record()  ← Part 2
```

**Key invariants:**
- `chunk_queue` and all main-thread delivery logic are **not modified**.
- `bytes` objects are immutable; putting the same reference into two queues has zero copy overhead.
- Only `bytes` chunks go into `usage_queue`. The `_ResponseHeaders` item placed first into `chunk_queue` is **not** put into `usage_queue`.
- SSE blank-line separator items (`b"\n"`, from `_encode_sse_line("")`) are `truthy` and ARE included — `UsageParser` ignores them (they don't match the `data: ` prefix).
- `usage_queue` is an unbounded `queue.Queue()`. `UsageParser` only reads from it; pricing is queried only after the sentinel is received, so no backpressure occurs during streaming.
- The `UsageParser` thread is a daemon; it exits when it receives the `None` sentinel or when the process exits.

### 2.2 Model Name Extraction

1. **Primary**: `handle_request()` parses the existing `body` variable (already assigned via `_get_request_body()`) and reads the top-level `"model"` field. Stored as `state.request_model` (plain `str`, empty string `""` if absent/unparseable). A `try/except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError)` guard is used; `request_model` defaults to `""` on any failure.
2. **Fallback**: `UsageParser` reads the `"model"` field from the first valid SSE `data:` chunk where `request_model` is empty.
3. **Final fallback**: model recorded as `"unknown"`, cost recorded as `None`. USAGE log line is still written.

### 2.3 SSE vs Non-SSE Parsing

`UsageParser.run()` receives `is_sse: bool` extracted from the `_ResponseHeaders.is_sse` field when the thread is launched in `read_from_descriptors()`.

- **SSE mode (`is_sse=True`)**: Each `bytes` item in `usage_queue` is one complete SSE line produced by `_encode_sse_line()`, e.g. `b"data: {...}\n"` (with trailing `\n`) or `b"\n"` (blank separator). No cross-chunk buffering needed. `UsageParser` checks for a `data: ` prefix; after stripping the prefix it must also strip the trailing `\n` before attempting JSON parse: `line.removeprefix(b"data: ").rstrip(b"\n")`.
  - This no-buffering guarantee holds because the SSE path in `_streaming_worker` uses `response.iter_lines()`. If that path changes to `iter_bytes()`, line-buffer mode must be used regardless of `is_sse`.
- **Non-SSE mode (`is_sse=False`)**: Worker feeds raw binary chunks of arbitrary size. `UsageParser` maintains an internal line buffer, appending each chunk and splitting on `b"\n"`.

In both modes, `data: [DONE]` lines are skipped without JSON parse.

---

## 3. Components

### 3.1 `UsageRecord` dataclass

Location: `flow_proxy_plugin/utils/usage_parser.py`

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
    duration_ms: float              # time.monotonic() delta from start_time to None sentinel
```

`duration_ms` uses `time.monotonic()` from `state.start_time` (see §4.1 — `start_time` is changed to use `time.monotonic()`). `end_time = time.monotonic()` is recorded immediately after the `None` sentinel is received, before the `PricingCache` query.

### 3.2 `UsageParser` (`flow_proxy_plugin/utils/usage_parser.py`)

A standalone class. No significant constructor arguments — instantiated as `UsageParser()` in `read_from_descriptors()`.

```python
def run(
    self,
    usage_queue: "queue.Queue[bytes | None]",
    req_id: str,
    config_name: str,
    request_model: str,
    start_time: float,
    is_sse: bool,
) -> None:
```

Intended as the target of a daemon `threading.Thread`:

```python
parser = UsageParser()
t = threading.Thread(
    target=parser.run,
    args=(state.usage_queue, state.req_id, state.config_name,
          state.request_model, state.start_time, item.is_sse),
    daemon=True,
)
```

(`item` is the `_ResponseHeaders` loop variable in `read_from_descriptors()` at the `isinstance(item, _ResponseHeaders)` branch.)

All arguments are plain scalars or a queue — no reference to `StreamingState` or any main-thread object is held after the thread starts.

**Responsibilities:**
- Call `set_request_context(req_id, "WS")` at entry.
- Call `clear_request_context()` before exit on both normal and exception paths.
- Consume `bytes` chunks from `usage_queue` until `None` sentinel.
- SSE mode: each item is a complete line; check `data: ` prefix and JSON parse.
- Non-SSE mode: maintain a line buffer; split on `b"\n"` to extract complete lines.
- Skip `data: [DONE]` without JSON parse.
- From the **first** chunk containing a non-empty `"model"` field: store as fallback model (used only if `request_model` is empty).
- From the **last** chunk containing `"usage"` with non-null token counts: extract `prompt_tokens`, `completion_tokens`, `total_tokens`.
- After sentinel received: record `end_time = time.monotonic()` (before pricing query), compute `duration_ms = (end_time - start_time) * 1000`, build `UsageRecord`, query `PricingCache`, emit USAGE log line (§3.4), call `ProcessServices.get().usage_stats.record(...)` (Part 2 — the call site is implemented here, but `usage_stats` is added to `ProcessServices` in Part 3; the call must be present but is guarded with `hasattr` until Part 3 is complete — see §3.2 note below).
- Entire body wrapped in `try/except Exception`; any failure logs `ERROR` with `exc_info=True` and calls `clear_request_context()` before returning.

**`usage_stats.record()` call:**
```python
svc = ProcessServices.get()
if hasattr(svc, "usage_stats"):
    svc.usage_stats.record(
        model=record.model,
        config_name=record.config_name,
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        total_tokens=record.total_tokens,
        cost_usd=record.cost_usd,
        ts=datetime.now(),
    )
```
The `hasattr` guard is removed in Part 3 once `usage_stats` is unconditionally present on `ProcessServices`.

**Does NOT:**
- Hold any reference to `StreamingState`, `self.client`, `chunk_queue`, pipe fds, or any main-thread resource.
- Block the worker or main thread.
- Call `PricingCache` while consuming chunks (pricing query happens only after sentinel).

**Import note:** `usage_parser.py` calls `ProcessServices.get()` inside `run()` at runtime. No circular import: `process_services.py` → (will import) `usage_stats.py`; `usage_parser.py` → `process_services.py`. `process_services.py` must **not** import `usage_parser.py`.

### 3.3 `PricingCache` (`flow_proxy_plugin/utils/pricing_cache.py`)

A process-level singleton held by `ProcessServices` (added in Part 3). Instantiated as `PricingCache(target_base_url=...)`.

```python
class PricingCache:
    def __init__(self, target_base_url: str) -> None: ...

    def get_price(self, model: str) -> tuple[float, float] | None:
        """Returns (input_cost_per_token, output_cost_per_token) or None."""

    def reset(self) -> None:
        """Close dedicated httpx.Client and clear cache dict. For tests only."""
```

**Behaviour:**
- Owns a **dedicated `httpx.Client`** with `timeout=httpx.Timeout(connect=5.0, read=10.0)` — separate from `ProcessServices.http_client` to avoid races with `mark_http_client_dirty()`.
- Lazy-loads pricing on first request per model by calling `GET {target_base_url}/model/info`.
- **`/model/info` response schema:** `{"data": [{model_name, litellm_params, model_info: {input_cost_per_token, output_cost_per_token, ...}}]}`. Match `entry["model_name"] == model` or `entry["litellm_params"]["model"] == model`. Extract from `entry["model_info"]["input_cost_per_token"]` and `entry["model_info"]["output_cost_per_token"]`.
- Caches results per model name with a 1-hour TTL (wall clock, `time.time()`).
- Uses `threading.Lock` — double-checked pattern: check outside lock, then inside lock before HTTP fetch.
- On HTTP error, timeout, or malformed response: logs `WARNING`, returns `None`. Does not raise.
- `reset()` closes the dedicated `httpx.Client` and clears the cache dict.

**Usage in `UsageParser`:**
```python
svc = ProcessServices.get()
if hasattr(svc, "pricing_cache") and record.model != "unknown":
    price = svc.pricing_cache.get_price(record.model)
    if price is not None:
        input_cost, output_cost = price
        if record.prompt_tokens is not None and record.completion_tokens is not None:
            record.cost_usd = (
                record.prompt_tokens * input_cost
                + record.completion_tokens * output_cost
            )
```
The `hasattr` guards are removed in Part 3.

### 3.4 Log Format

One structured `INFO` log line emitted by `UsageParser` after stream completion (with `[req_id][WS]` prefix active):

```
[abc123][WS] USAGE model=claude-3-5-sonnet config=prod-1 prompt_tokens=1024 completion_tokens=512 total_tokens=1536 cost_usd=0.003456 duration_ms=4231
```

Fields with `None` values are omitted (e.g. `cost_usd` omitted when pricing unavailable; token fields omitted when usage absent in response).

---

## 4. `StreamingState` Changes

### 4.1 `start_time` clock change: `time.time()` → `time.monotonic()`

**Current code:** `start_time` already exists in `StreamingState` (line 65 of `web_server_plugin.py`) and is set via `time.time()` in `handle_request()` (line 269) and read in `_finish_stream()` and `_reset_request_state()` for `duration` calculations.

**Required change:** Switch `start_time` from `time.time()` to `time.monotonic()` in `handle_request()` (line 269). The `_finish_stream()` and `_reset_request_state()` callers must also change their `duration` calculations from `time.time() - state.start_time` to `time.monotonic() - state.start_time`.

**Rationale:** `UsageParser` computes `duration_ms` using `time.monotonic()` delta from `start_time`. Using a monotonic clock is also more correct for elapsed-time calculations (immune to wall-clock adjustments). The existing log format for `duration` (currently seconds, e.g. `duration=%.1fs`) is unchanged; only the clock source changes.

**All affected lines in `web_server_plugin.py`:**
- Line 65: inline comment on `start_time` field: `# set in handle_request() via time.time()` → `# set in handle_request() via time.monotonic()`
- Line 269: `start_time = time.time()` → `start_time = time.monotonic()`
- Line 204: `duration = time.time() - state.start_time` → `duration = time.monotonic() - state.start_time`
- Line 213: same change
- Line 245: same change
- Worker lines 533, 553: `state.ttfb = time.time() - state.start_time` → `state.ttfb = time.monotonic() - state.start_time`

### 4.2 New `StreamingState` fields

Three new fields added to the existing `StreamingState` dataclass. The `start_time` field already exists as a required field (no change to its position). The new `usage_queue` required field is inserted **before the first existing defaulted field** (`headers_sent`). The two new optional fields are appended at the end.

**Dataclass definition field order** (the actual Python dataclass source must follow this exact order to satisfy Python's rule that required fields cannot follow fields with defaults):

```python
@dataclass
class StreamingState:
    # --- existing required fields (unchanged) ---
    pipe_r: int
    pipe_w: int
    chunk_queue: "queue.Queue[_ResponseHeaders | bytes | None]"
    thread: threading.Thread | None
    cancel: threading.Event
    req_id: str
    config_name: str
    start_time: float          # clock changed to time.monotonic() (§4.1)
    stream: bool | None
    # --- NEW required field (must appear before any defaulted field) ---
    usage_queue: "queue.Queue[bytes | None]"
    # --- existing defaulted fields (unchanged) ---
    headers_sent: bool = False
    is_sse: bool = False
    status_code: int = 0
    error: BaseException | None = None
    ttfb: float | None = None
    bytes_sent: int = 0
    # --- NEW optional fields (appended after all existing defaulted fields) ---
    request_model: str = ""
    usage_parser_thread: "threading.Thread | None" = None
```

**Construction sites**: all callers pass arguments as keyword arguments, so argument order in call sites does not need to match the dataclass field order. The only requirement is that `usage_queue=` is now a required keyword argument. See Part 3 §4.1 for all construction sites.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `usage_queue` | `queue.Queue[bytes \| None]` | required | Independent channel from worker to `UsageParser`. Created per-request in `handle_request()`. |
| `request_model` | `str` | `""` | Model name extracted from request body; empty string if absent. |
| `usage_parser_thread` | `threading.Thread \| None` | `None` | Set by `read_from_descriptors()` when `_ResponseHeaders` is first dequeued; joined by `_reset_request_state()` in Part 3. |

**Note:** The existing `ttfb` field keeps its current name (`ttfb`, not `ttfb_ms`). The original spec used `ttfb_ms` but the current code already has `ttfb`. All new code in this spec uses `state.ttfb` to stay consistent.

---

## 5. Integration Points

### 5.1 `handle_request()` changes

**Execution order in `handle_request()` (for orientation):**
```
1. req_id / start_time / stream parsing
2. config_name = ""  ← Part 3 addition
3. try: _, config_name, jwt_token = _get_config_and_token()  ← auth (may fail)
4. body = self._get_request_body(request, filter_rule)  ← AFTER auth
5. request_model extracted from body  ← steps below (AFTER body exists)
6. usage_queue = queue.Queue()
7. StreamingState(..., usage_queue=..., request_model=...)
8. state.thread.start()
```

Steps 5–8 all take place after auth succeeds. `body` is only available at step 4 and later.

**Changes for Part 1** (steps 5–7, inserting after the existing `body = self._get_request_body(...)` line):

1. **Extract `request_model`** from the existing `body` variable. Do not call `_get_request_body()` again. `body` is `bytes | None`; use a `try/except` around the JSON parse:
   ```python
   request_model = ""
   if body:
       try:
           parsed = json.loads(body)
           if isinstance(parsed, dict):
               request_model = parsed.get("model") or ""
       except (json.JSONDecodeError, UnicodeDecodeError):
           pass
   ```

2. **Create `usage_queue`**: `usage_queue: queue.Queue[bytes | None] = queue.Queue()`

3. **Pass to `StreamingState`**: add `usage_queue=usage_queue` and `request_model=request_model` to the `StreamingState(...)` constructor call. The `start_time` argument already exists; its clock source is changed to `time.monotonic()` (§4.1).

4. **After `state.thread.start()`**: call `record_stream_event("started", ...)` — this is implemented in Part 3. No change needed here in Part 1.

### 5.2 `_streaming_worker()` changes

The exact per-chunk ordering (applies to BOTH SSE and non-SSE loops) — add `usage_queue.put` immediately after each `chunk_queue.put`:

```python
# First chunk only (ttfb block — clock changed to monotonic):
if state.ttfb is None:
    state.ttfb = time.monotonic() - state.start_time
    ...

# Every chunk (both paths):
state.chunk_queue.put(chunk)
state.usage_queue.put(chunk)   # NEW — immediately after chunk_queue.put
try:
    os.write(state.pipe_w, b"\x00")
except OSError:
    return
```

In the `finally` block, add `state.usage_queue.put(None)` **immediately after** `chunk_queue.put(None)` and **before** `os.write(state.pipe_w, b"\x00")`:

```python
# finally block:
state.chunk_queue.put(None)
state.usage_queue.put(None)   # NEW
try:
    os.write(state.pipe_w, b"\x00")
except OSError:
    pass
```

`usage_queue.put(None)` before `os.write()` ensures the `UsageParser` sentinel is enqueued before the main thread wakes. The `finally` block runs on ALL exit paths.

The `_ResponseHeaders` item put into `chunk_queue` at the top of the worker is **not** put into `usage_queue`.

**Empty chunk filtering (non-SSE path):** The non-SSE loop already has `if not chunk: continue` which skips empty chunks before `chunk_queue.put(chunk)`. Because `usage_queue.put(chunk)` is placed immediately after `chunk_queue.put(chunk)`, empty chunks are never put into `usage_queue` either — the `continue` prevents reaching both puts. No additional guard needed.

### 5.3 `read_from_descriptors()` changes

When the `_ResponseHeaders` item is first dequeued (the `isinstance(item, _ResponseHeaders)` branch), launch the `UsageParser` daemon thread after all header processing completes (i.e., after `state.headers_sent = True`):

```python
if isinstance(item, _ResponseHeaders):
    state.is_sse = item.is_sse
    state.status_code = item.status_code
    self._send_response_headers_from(item)
    state.headers_sent = True
    # Launch UsageParser after header processing is complete
    if state.usage_parser_thread is None:
        from ..utils.usage_parser import UsageParser  # avoid circular import at module level
        parser = UsageParser()
        t = threading.Thread(
            target=parser.run,
            args=(state.usage_queue, state.req_id, state.config_name,
                  state.request_model, state.start_time, item.is_sse),
            daemon=True,
        )
        state.usage_parser_thread = t
        t.start()
```

The `if state.usage_parser_thread is None:` guard is a safety measure — `_ResponseHeaders` is enqueued exactly once, so the guard will never fire twice in practice. The thread is launched **after** `state.headers_sent = True` so that all header state is consistent.

**Import placement**: The `from ..utils.usage_parser import UsageParser` import can be placed at the module level in `web_server_plugin.py`. Full import chain audit:

- `web_server_plugin.py` → `usage_parser.py` (module level) — new
- `usage_parser.py` → `process_services.py` (inside `run()` method body, not at module level) — no module-level cycle
- `process_services.py` → `.pricing_cache`, `.usage_stats` (Part 3 additions) — both are new modules with no back-imports to `web_server_plugin.py` or `usage_parser.py`
- `usage_parser.py` must **not** be imported by `process_services.py` at module level (that would create a cycle: `web_server_plugin` → `usage_parser` → `process_services` → `usage_parser`)

Conclusion: module-level import of `UsageParser` in `web_server_plugin.py` is safe.

**Impact on existing `TestEventLoopHooks` tests**: Several existing tests (`test_read_from_descriptors_queues_each_chunk`, `test_read_from_descriptors_sends_headers_on_first_item`, `test_read_from_descriptors_tracks_bytes_sent`, `test_read_from_descriptors_returns_true_on_sentinel`, etc.) pre-fill `state.chunk_queue` with a `_ResponseHeaders` item and then call `read_from_descriptors()`. After this change, that code path will attempt to start a `UsageParser` thread. The `UsageParser` thread will immediately block on `state.usage_queue.get()` indefinitely (because no worker is running to fill the queue), and `_reset_request_state()` will waste 2 seconds joining it on timeout.

All such tests must be updated in Part 3 (§4.3) to prevent this. The fix for each test is to patch `UsageParser` so that the thread does not block:
```python
from unittest.mock import patch, MagicMock
with patch("flow_proxy_plugin.plugins.web_server_plugin.UsageParser") as mock_parser_cls:
    mock_parser_cls.return_value.run = MagicMock()   # no-op run()
    asyncio.run(plugin.read_from_descriptors([pipe_r]))
```
See Part 3 §4.3 for the complete list of tests that require this patch.

### 5.4 `_reset_request_state()` changes (Part 1 portion)

Add a join for `usage_parser_thread` **after** the worker thread join. The `record_stream_event()` call on client disconnect is added in Part 3.

```python
# After existing: state.thread.join(timeout=2.0)
if state.usage_parser_thread is not None:
    state.usage_parser_thread.join(timeout=2.0)
```

`usage_parser_thread` may be `None` if the client disconnected before `read_from_descriptors()` dequeued the `_ResponseHeaders` item. In that case the join is skipped.

**Join ordering:** Worker thread is joined first, then `UsageParser` thread. This ensures the worker's `finally` block (which delivers `usage_queue.put(None)`) has completed before the `UsageParser` join begins.

---

## 6. Failure Handling

### 6.1 Missing `usage` in Response

`UsageParser` logs the USAGE line with token fields omitted. No exception.

### 6.2 `PricingCache` Failure

HTTP error, timeout, or malformed response from `/model/info`. `WARNING` logged, `get_price()` returns `None`, cost omitted. Next request for the same model will retry (no negative TTL).

### 6.3 `UsageParser` Thread Exception

Any unhandled exception inside `run()`: `ERROR` logged with `exc_info=True`, `clear_request_context()` called, thread exits. No impact on main pipeline or worker.

### 6.4 Client Disconnect Before Stream Ends

The worker's `finally` block puts `None` into both `chunk_queue` and `usage_queue`. `_reset_request_state()` joins the worker first (ensuring `usage_queue.put(None)` has run), then joins `usage_parser_thread`. The `UsageParser` holds only plain scalar values captured at launch — no reference to `StreamingState` or the plugin instance — so it is safe even if the pool instance is reused.

### 6.5 Stream Setup Failure

`_streaming_state` is set to `None` and `usage_parser_thread` is never started (it is launched in `read_from_descriptors()`, which only runs when streaming is active). No cleanup needed.

---

## 7. Testing

### 7.1 New: `tests/test_usage_parser.py`

| Test | Assertion |
|------|-----------|
| `test_parse_usage_from_sse_chunks` | Feed pre-encoded SSE lines including final usage chunk (`is_sse=True`); assert correct `prompt_tokens`, `completion_tokens`, `total_tokens` extracted. |
| `test_parse_usage_non_sse` | Feed raw bytes with partial-line splits (`is_sse=False`); assert correct token counts via line buffering. |
| `test_parse_model_from_first_chunk` | `request_model=""`; first chunk has `"model"` field; assert extracted as fallback model. |
| `test_request_model_takes_priority` | `request_model="req-model"`; response also has `"model": "resp-model"`; assert `"req-model"` used. |
| `test_missing_usage_graceful` | All chunks lack usage; assert `UsageRecord` has `None` tokens and no `cost_usd`, no exception. |
| `test_unknown_model_no_cost` | model `"unknown"`, pricing unavailable; assert `cost_usd=None`, USAGE log line written. |
| `test_done_sentinel_ignored` | `data: [DONE]` line does not cause JSON parse error. |
| `test_parser_exception_logged_and_context_cleared` | Inject a queue that raises on `get()`; assert `ERROR` logged with `exc_info`, `clear_request_context()` called. |
| `test_set_and_clear_request_context` | Assert `set_request_context` called at entry and `clear_request_context` called at exit on both normal and exception paths. |
| `test_duration_ms_uses_monotonic` | Assert `duration_ms` is computed using `time.monotonic()` delta (mock `time.monotonic` to return deterministic values). |

### 7.2 New: `tests/test_pricing_cache.py`

| Test | Assertion |
|------|-----------|
| `test_fetch_and_cache` | First call hits HTTP (`/model/info`); second call for same model uses cache, no HTTP call. |
| `test_ttl_expiry_triggers_refetch` | Artificially set `_cache[model]["expires_at"]` to the past; next call re-fetches. |
| `test_failure_returns_none` | `/model/info` returns HTTP 500; `get_price()` returns `None`, logs `WARNING`. |
| `test_timeout_returns_none` | `/model/info` raises `httpx.TimeoutException`; `get_price()` returns `None`, logs `WARNING`. |
| `test_thread_safe_concurrent_access` | 10 threads call `get_price()` concurrently; no corruption; HTTP called exactly once. |
| `test_reset_clears_cache` | `reset()` clears all cached entries; next call re-fetches. |
| `test_model_info_json_path` | Mock response with `{"data": [{"model_name": "m", "litellm_params": {}, "model_info": {"input_cost_per_token": 0.001, "output_cost_per_token": 0.002}}]}`; assert `get_price("m") == (0.001, 0.002)`. |
| `test_litellm_params_model_fallback` | `data[].model_name` does not match but `data[].litellm_params.model` does; assert correct price returned. |

---

## 8. Backward Compatibility

- No changes to environment variables, request/response wire format, or public API.
- `StreamingState` gains one new required field (`usage_queue`) which must be supplied at all construction sites. Construction sites:
  - Production: `handle_request()` in `web_server_plugin.py` — updated in §5.1.
  - Tests: 4 construction sites in `tests/test_web_server_plugin.py` — updated in Part 3 spec §5.
- `start_time` clock change (`time.time()` → `time.monotonic()`) affects only elapsed-time calculations; the log output format (`duration=%.1fs`) is unchanged.
- The `ttfb` field keeps its existing name (`ttfb`, not `ttfb_ms`). No rename.

---

## 9. References

- Original combined spec: `docs/superpowers/specs/2026-03-14-usage-stats-design.md`
- Part 2 (UsageStats persistence): `docs/superpowers/specs/2026-03-15-usage-stats-part2-usage-stats-design.md`
- Part 3 (ProcessServices + test fixes): `docs/superpowers/specs/2026-03-15-usage-stats-part3-integration-design.md`
- LiteLLM pricing endpoint: `GET /model/info` → `data[].model_info.{input_cost_per_token, output_cost_per_token}`
- B3 async streaming design: `docs/superpowers/specs/2026-03-13-async-streaming-design.md`
