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

- **Persistent stats aggregation**
  In-memory counters flushed to `~/.flow-proxy/stats.json` every N minutes (default 5 min) with three time granularities: `hourly` (last 7 days), `daily` (indefinite), and `all_time` (cumulative).

- **Full streaming observability** (implements robustness spec §4.2 Phase 1)
  Track all metric semantics defined in `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md §4.2`: `stream_requests_total`, `stream_responses_total` (by status class), `stream_errors_total` (by error reason), `stream_ttfb_ms`, `stream_duration_ms` — covering all exit paths including auth failure, setup failure, worker error, and client disconnect.

### 1.2 Non-Goals

- Real-time aggregation dashboards or Prometheus metrics export (Phase 2).
- Per-user or per-team budgeting.
- Non-streaming requests (forward proxy / `FlowProxyPlugin`); can be added later.
- Modifying client requests to inject `stream_options: {include_usage: true}` — this is a separate concern; the design degrades gracefully when usage is absent.

### 1.3 Scope

- Applies only to the **B3 async streaming path**: `FlowProxyWebServerPlugin`.
- Three new modules: `flow_proxy_plugin/utils/usage_parser.py`, `flow_proxy_plugin/utils/pricing_cache.py`, `flow_proxy_plugin/utils/usage_stats.py`.
- Minor additions to `StreamingState`, `ProcessServices`, `_streaming_worker`, and `LogCleaner`.

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
| `ttfb_ms` | `float \| None` | `None` | Set by worker on first byte/line; read by `_finish_stream()` after worker join (GIL-visibility, same pattern as `state.error`). |

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

## 6. Stats Persistence (`~/.flow-proxy/stats.json`)

### 6.1 Architecture: In-Memory Counters + Periodic Flush

Stats persistence uses a two-tier approach:

1. **In-process tier** (`UsageStats` singleton in `ProcessServices`): `UsageParser` calls `usage_stats.record()` after building a `UsageRecord`. This is a pure in-memory operation (dict increment + `threading.Lock`), adding negligible overhead per request.
2. **Disk tier**: A `StatsFlushThread` daemon inside `UsageStats` wakes every `flush_interval` seconds (default 300 s, configurable via `FLOW_PROXY_STATS_FLUSH_INTERVAL` env var) and calls `usage_stats.flush(stats_file)`. Each process flushes its own incremental counters into `~/.flow-proxy/stats.json` with `fcntl.flock` to serialize multi-process writes.

**Why this design handles high concurrency:**
- The per-request hot path is lock-protected in-memory dict operations only — no I/O, no file contention.
- Write frequency to disk is decoupled from QPS; a burst of 1000 req/s still results in only one flush per `flush_interval` period.
- Multi-process safety: each process's flush is a read-increment-write cycle protected by an exclusive POSIX file lock.

**Data loss risk:** at most `flush_interval` seconds of data on hard crash (acceptable for usage statistics).

### 6.2 `stats.json` Schema

Location: `~/.flow-proxy/stats.json` (created with parent dir on first flush).

```json
{
  "version": 1,
  "last_flushed_at": "2026-03-14T12:00:00",
  "all_time": {
    "stream_requests": 1234,
    "stream_responses": 1100,
    "stream_errors": 134,
    "responses_by_status_class": {"2xx": 980, "4xx": 100, "5xx": 20},
    "errors_by_reason": {
      "auth_error": 10, "setup_failed": 5,
      "transport_error": 80, "worker_error": 30, "client_disconnect": 9
    },
    "ttfb_ms_sum": 250000,
    "ttfb_ms_count": 1100,
    "duration_ms_sum": 5000000,
    "duration_ms_count": 1234,
    "total_prompt_tokens": 1000000,
    "total_completion_tokens": 500000,
    "total_tokens": 1500000,
    "total_cost_usd": 45.67,
    "by_model": {
      "claude-3-5-sonnet": {
        "stream_requests": 800,
        "total_prompt_tokens": 700000,
        "total_completion_tokens": 350000,
        "total_tokens": 1050000,
        "total_cost_usd": 30.0
      }
    },
    "by_config": {
      "prod-1": {
        "stream_requests": 600,
        "total_prompt_tokens": 500000,
        "total_completion_tokens": 250000,
        "total_tokens": 750000,
        "total_cost_usd": 22.5
      }
    }
  },
  "daily": {
    "2026-03-14": { "...same bucket structure..." }
  },
  "hourly": {
    "2026-03-14T12": { "...same bucket structure..." }
  }
}
```

**Bucket structure** (shared by `all_time`, each `daily` entry, each `hourly` entry):

| Field | Type | Call site | Notes |
|-------|------|-----------|-------|
| `stream_requests` | int | `handle_request()` after worker starts | All requests where worker thread started. |
| `stream_responses` | int | `_finish_stream()` on no error | Requests that received ≥1 chunk from backend. |
| `stream_errors` | int | All error exit paths | Auth failure, setup failure, worker error, client disconnect. |
| `responses_by_status_class` | dict[str, int] | `_finish_stream()` on no error | Keys: `"2xx"`, `"4xx"`, `"5xx"`. |
| `errors_by_reason` | dict[str, int] | All error exit paths | Keys: `"auth_error"`, `"setup_failed"`, `"transport_error"`, `"worker_error"`, `"client_disconnect"`. |
| `ttfb_ms_sum` | float | `_finish_stream()` | Sum of TTFB values for computing average. `None` skipped. |
| `ttfb_ms_count` | int | `_finish_stream()` | Count of requests with non-None TTFB. |
| `duration_ms_sum` | float | `_finish_stream()` + `_reset_request_state()` | Sum of all durations. |
| `duration_ms_count` | int | Same | Count of requests with duration. |
| `total_prompt_tokens` | int | `UsageParser` via `record()` | Sum; `None` skipped. |
| `total_completion_tokens` | int | Same | Same. |
| `total_tokens` | int | Same | Same. |
| `total_cost_usd` | float | Same | Sum; `None` skipped. |
| `by_model` | dict[str, bucket] | `UsageParser` via `record()` | Per-model breakdown: `stream_requests`, token fields, `total_cost_usd`. |
| `by_config` | dict[str, bucket] | Both paths | Per-config breakdown: same fields as top-level bucket. |

**Retention policy:**
- `all_time`: cumulative, never trimmed.
- `daily`: kept indefinitely.
- `hourly`: trimmed to last 7 days (168 entries max) on each flush.

### 6.3 `UsageStats` (`flow_proxy_plugin/utils/usage_stats.py`)

A process-level singleton held by `ProcessServices`.

```python
class UsageStats:
    def record(
        self,
        model: str,
        config_name: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        cost_usd: float | None,
        ts: datetime,
    ) -> None:
        """Token/cost data from UsageParser. Thread-safe in-memory increment. No I/O."""

    def record_stream_event(
        self,
        config_name: str,
        event: str,                     # "started" | "response" | "error"
        status_code: int | None,        # for "response" event
        error_reason: str | None,       # for "error" event; see errors_by_reason keys
        ttfb_ms: float | None,          # for "response" event; None if not measured
        duration_ms: float | None,      # for "response" and "error" events
        ts: datetime,
    ) -> None:
        """Stream lifecycle event from main thread. Thread-safe in-memory increment. No I/O."""

    def flush(self, stats_file: Path) -> None:
        """Merge in-memory increments into stats_file and reset counters."""

    def reset(self) -> None:
        """Clear in-memory counters and stop flush thread. Tests only."""
```

**`record()` internals:**
- Acquires `self._lock` (brief critical section: a few dict lookups and integer increments).
- Updates three buckets: `_pending["all_time"]`, `_pending["daily"][ts.strftime("%Y-%m-%d")]`, `_pending["hourly"][ts.strftime("%Y-%m-%dT%H")]`.
- Increments `stream_requests` (via `by_model` and `by_config`), token fields.

**`record_stream_event()` internals:**
- Same lock and three-bucket update.
- `"started"`: increments `stream_requests` and `by_config[config_name].stream_requests`.
- `"response"`: increments `stream_responses`, `responses_by_status_class[status_class]`, adds to `ttfb_ms_sum/count` and `duration_ms_sum/count`.
- `"error"`: increments `stream_errors`, `errors_by_reason[error_reason]`, adds to `duration_ms_sum/count` if `duration_ms` is not None.

**`flush()` internals:**
1. Under `self._lock`, swap `self._pending` with a fresh empty dict (atomic swap — minimises lock hold time; in-flight `record()` calls on other threads see the new empty dict immediately after).
2. If the swapped snapshot is empty, return early (no I/O).
3. Open `stats_file` path (creating parent dir if absent); acquire `fcntl.flock(fd, LOCK_EX)` on a dedicated lock file `stats_file.with_suffix(".lock")`.
4. Read existing `stats.json` (empty structure if absent or corrupt).
5. Add snapshot increments to the file's existing values (merge bucket by bucket).
6. Trim `hourly` keys older than 7 days.
7. Update `last_flushed_at = datetime.now().isoformat(timespec="seconds")`.
8. Atomic write: serialise to `stats_file.with_suffix(".tmp")`, then `os.replace()`.
9. Release lock.

**`StatsFlushThread`** is a daemon `threading.Thread` started by `UsageStats.__init__()`. It calls `flush()` every `flush_interval` seconds (read from `FLOW_PROXY_STATS_FLUSH_INTERVAL` env var, default 300, valid range 10–3600). The thread exits on `reset()` or process exit.

### 6.4 `StreamingState` addition for TTFB

One new field added to `StreamingState`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ttfb_ms` | `float \| None` | `None` | Set by worker on first byte/line using `(time.monotonic() - state.start_time) * 1000`. Read by main thread in `_finish_stream()` after worker join. Thread-safety: same GIL-visibility pattern as `state.error`. |

The worker sets `state.ttfb_ms` immediately before logging `"Received first SSE line…"` / `"Received first chunk…"`.

### 6.5 `record_stream_event()` Call Sites

| Call site | Event | Fields set |
|-----------|-------|------------|
| `handle_request()` — after `state.thread.start()` | `"started"` | `config_name`, `ts` |
| `handle_request()` — auth failure before return | `"error"` | `config_name`, `error_reason="auth_error"`, `ts` |
| `handle_request()` — setup failure before return | `"error"` | `config_name`, `error_reason="setup_failed"`, `ts` |
| `_finish_stream()` — `state.error is None` | `"response"` | `config_name`, `status_code`, `ttfb_ms=state.ttfb_ms`, `duration_ms`, `ts` |
| `_finish_stream()` — `isinstance(state.error, httpx.TransportError)` | `"error"` | `config_name`, `error_reason="transport_error"`, `duration_ms`, `ts` |
| `_finish_stream()` — other `state.error` | `"error"` | `config_name`, `error_reason="worker_error"`, `duration_ms`, `ts` |
| `_reset_request_state()` — client disconnect | `"error"` | `config_name`, `error_reason="client_disconnect"`, `duration_ms=(time.monotonic()-state.start_time)*1000`, `ts` |

`duration_ms` in `_finish_stream()` is `(time.monotonic() - state.start_time) * 1000`, same calculation as `UsageParser`.

`ProcessServices.get().usage_stats.record_stream_event(...)` is called from the main thread on all paths; no thread-boundary concerns.

### 6.6 Integration with `UsageParser`

After building `UsageRecord`, `UsageParser.run()` calls:

```python
ProcessServices.get().usage_stats.record(
    model=record.model,
    config_name=record.config_name,
    prompt_tokens=record.prompt_tokens,
    completion_tokens=record.completion_tokens,
    total_tokens=record.total_tokens,
    cost_usd=record.cost_usd,
    ts=datetime.now(),
)
```

This call happens after the USAGE log line is emitted. If `record()` raises (should never happen), the exception is swallowed by the existing `try/except Exception` wrapper in `run()`.

### 6.8 Integration with `LogCleaner`

`LogCleaner` receives a `usage_stats: UsageStats | None = None` constructor argument. When set, `cleanup_logs()` calls `usage_stats.flush(stats_file)` **once before** any `log_file.unlink()` call. This ensures in-memory data accumulated since the last periodic flush is persisted before old logs are removed.

`init_log_cleaner()` and `LogSetup.initialize_cleaner()` pass `usage_stats=ProcessServices.get().usage_stats` and `stats_file=Path.home() / ".flow-proxy" / "stats.json"` by default.

`LogCleaner` constructor additions:

```python
def __init__(
    self,
    *,
    log_dir: Path,
    retention_days: int = 7,
    cleanup_interval_hours: int = 24,
    max_size_mb: int = 0,
    enabled: bool = True,
    usage_stats: "UsageStats | None" = None,   # new
    stats_file: Path | None = None,             # new
):
```

### 6.9 `ProcessServices` additions

- Add `usage_stats: UsageStats` attribute, initialised in `_initialize()` with `stats_file=Path.home() / ".flow-proxy" / "stats.json"` and `flush_interval` from env.
- Add `usage_stats.reset()` in `reset()` for test isolation.

### 6.10 Testing (`tests/test_usage_stats.py`)

| Test | Assertion |
|------|-----------|
| `test_record_updates_all_buckets` | Single `record()` call; assert `_pending` has `all_time`, `daily`, `hourly` entries with correct values. |
| `test_record_thread_safe` | 100 threads each call `record()` 10 times concurrently; assert `total_requests == 1000`. |
| `test_flush_creates_stats_file` | No `stats.json`; `flush()` creates it with correct structure. |
| `test_flush_merges_increments` | Existing `stats.json` with `total_requests=5`; `flush()` with 3 new requests; assert `total_requests==8`. |
| `test_flush_resets_pending` | After `flush()`, `_pending` is empty; second `flush()` is a no-op (no file write). |
| `test_flush_atomic_write` | Mock `os.replace` to raise; assert original `stats.json` unchanged. |
| `test_flush_multiprocess_safe` | Two threads call `flush()` concurrently on the same file; assert no data corruption or lost increments. |
| `test_hourly_trimmed_to_7_days` | Record events with 200 distinct hours; after `flush()`, only last 168 hourly keys remain. |
| `test_none_fields_skipped` | `record()` with `None` tokens; assert token fields not present in pending bucket. |
| `test_flush_interval_env_var` | `FLOW_PROXY_STATS_FLUSH_INTERVAL=10`; assert `StatsFlushThread` fires within ~15 s. |
| `test_log_cleaner_flushes_before_delete` | Mock `usage_stats.flush`; assert called before `unlink()` in `cleanup_logs()`. |
| `test_record_stream_event_started` | `record_stream_event(event="started", ...)`; assert `stream_requests==1`, no `stream_errors`. |
| `test_record_stream_event_response` | `record_stream_event(event="response", status_code=200, ttfb_ms=123.0, duration_ms=4000.0)`; assert `stream_responses==1`, `responses_by_status_class["2xx"]==1`, `ttfb_ms_sum==123.0`, `ttfb_ms_count==1`. |
| `test_record_stream_event_error_reasons` | One event per error_reason; assert `stream_errors==5`, each key in `errors_by_reason==1`. |
| `test_record_stream_event_4xx_status_class` | `status_code=429`; assert `responses_by_status_class["4xx"]==1`. |
| `test_handle_request_records_started_and_errors` | Mock `usage_stats`; assert `record_stream_event("started")` called after `thread.start()`; assert `record_stream_event("error", error_reason="auth_error")` on auth failure. |
| `test_finish_stream_records_response` | Mock `usage_stats`; assert `record_stream_event("response", status_code=200)` called in `_finish_stream()` when no error. |
| `test_finish_stream_records_transport_error` | Mock `usage_stats`; `state.error = httpx.TransportError(...)`; assert `record_stream_event("error", error_reason="transport_error")`. |
| `test_reset_request_state_records_client_disconnect` | Mock `usage_stats`; call `_reset_request_state()`; assert `record_stream_event("error", error_reason="client_disconnect")`. |
| `test_worker_sets_ttfb_ms` | Worker thread sets `state.ttfb_ms` on first byte; assert non-None after stream completes. |

---

## 7. Testing

### 7.1 New: `tests/test_usage_parser.py`

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

### 7.2 New: `tests/test_pricing_cache.py`

| Test | Assertion |
|------|-----------|
| `test_fetch_and_cache` | First call hits HTTP (`/model/info`); second call for same model uses cache, no HTTP. |
| `test_ttl_expiry_triggers_refetch` | Artificially expire TTL; next call re-fetches. |
| `test_failure_returns_none` | `/model/info` returns 500; `get_price()` returns `None`, logs `WARNING`. |
| `test_timeout_returns_none` | `/model/info` times out; `get_price()` returns `None`, logs `WARNING`. |
| `test_thread_safe_concurrent_access` | Multiple threads call `get_price()` concurrently; no corruption; HTTP called exactly once. |
| `test_reset_clears_cache` | `reset()` clears all cached entries; next call re-fetches. |
| `test_model_info_json_path` | Mock response with nested `data[].model_info.input_cost_per_token`; assert correct values extracted. |

### 7.3 Modified: `tests/test_web_server_plugin.py`

| Test | Change |
|------|--------|
| `test_streaming_state_defaults` | Assert `usage_queue` is a `Queue`, `start_time` is `float`, `request_model` defaults to `""`. Assert `usage_parser_thread` defaults to `None`. |
| `test_handle_request_launches_worker` | Assert `usage_queue` created and stored in `StreamingState`; `UsageParser` thread started in `read_from_descriptors()` after headers dequeued. |
| `test_worker_sentinel_delivered_to_usage_queue` | After worker completes, assert `usage_queue` contains `None` sentinel (worker `finally` delivers it). |
| `test_reset_request_state_join_order` | After `_reset_request_state()`, assert worker thread joined before `UsageParser` thread. |

### 7.4 Modified: `tests/test_process_services.py`

- Assert `pricing_cache` is initialised in `ProcessServices.get()`.
- Assert `pricing_cache.reset()` called in `ProcessServices.reset()`.
- Assert `usage_stats` is initialised in `ProcessServices.get()`.
- Assert `usage_stats.reset()` called in `ProcessServices.reset()`.

---

## 7. Backward Compatibility

- No configuration changes to existing env vars; no new environment variables.
- No changes to request or response wire format.
- `PricingCache` uses `target_base_url` from `RequestForwarder`; no new secrets or endpoints required.
- `usage_queue` and `start_time` are required `StreamingState` constructor arguments (no default); `request_model` defaults to `""`; `usage_parser_thread` defaults to `None`. `usage_queue` and `start_time` are constructed in `handle_request()`; `usage_parser_thread` is set in `read_from_descriptors()`.
- `StreamingState` gains `ttfb_ms: float | None = None`; existing construction code unchanged (default handles it).
- `LogCleaner` gains two optional constructor arguments (`usage_stats`, `stats_file`), both defaulting to `None`; existing callers are unaffected. When both are provided, `cleanup_logs()` calls `usage_stats.flush(stats_file)` once before deletions — no log file scanning.
- New env var `FLOW_PROXY_STATS_FLUSH_INTERVAL` (default 300 s, range 10–3600); all other env vars unchanged.
- `~/.flow-proxy/` directory is created on first flush; no pre-existing configuration required.

---

## References

- Streaming robustness design: `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md`
- B3 async streaming design: `docs/superpowers/specs/2026-03-13-async-streaming-design.md`
- LiteLLM streaming usage: `stream_options: {include_usage: true}` / proxy `always_include_stream_usage: true`
- LiteLLM pricing endpoint: `GET /model/info` → `data[].model_info.{input_cost_per_token, output_cost_per_token}`; match by `data[].model_name` or `data[].litellm_params.model`
- Log file format: `%(asctime)s - %(name)s - %(levelname)s - %(message)s` (date format `%Y-%m-%d %H:%M:%S`); rotated daily with suffix `YYYY-MM-DD`
- `stats.json` location: `~/.flow-proxy/stats.json`
