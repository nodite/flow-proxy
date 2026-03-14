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
  httpx stream ─────►│  for chunk in response:                    │
                     │    chunk_queue.put(chunk)  ← main pipeline  │
                     │    usage_queue.put(chunk)  ← new, bytes only│
                     │                                             │
                     │  finally:                                   │
                     │    chunk_queue.put(None)   ← unchanged      │
                     │    usage_queue.put(None)   ← new sentinel   │
                     └─────────────────────────────────────────────┘
                             │                      │
                             ▼                      ▼
                  chunk_queue (unchanged)    usage_queue (new)
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
- The `UsageParser` thread is a daemon; it exits when the process exits or when it receives the `None` sentinel.

### 2.2 Model Name Extraction (A+B combined)

1. **Primary**: `handle_request()` parses the request body JSON and reads the top-level `"model"` field. Stored in `StreamingState.request_model`.
2. **Fallback**: `UsageParser` reads the `"model"` field from the first valid SSE `data:` chunk if `request_model` is absent or empty.
3. **Final fallback**: model recorded as `"unknown"`, cost recorded as `None`. Log line is still written.

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
    duration_ms: float              # monotonic, handle_request → worker done
```

### 3.2 `UsageParser` (`flow_proxy_plugin/utils/usage_parser.py`)

A standalone class with a single public method `run(usage_queue, request_model, state_ref)` intended to be the target of a daemon `threading.Thread`.

**Responsibilities:**
- Consume raw `bytes` chunks from `usage_queue` until `None` sentinel.
- Decode each chunk as UTF-8 and split on newlines; scan for `data: {...}` lines.
- From the **first** chunk containing `"model"`: extract model name (used as fallback if `request_model` is absent).
- From the **last** non-`[DONE]` chunk containing `"usage"` with a non-null value: extract `prompt_tokens`, `completion_tokens`, `total_tokens`.
- After sentinel received: build `UsageRecord`, query `PricingCache`, emit log line.
- Entire body wrapped in `try/except Exception`; any failure logs `ERROR` and returns.

**Does NOT:**
- Touch `self.client`, `chunk_queue`, pipe fds, or any other main-thread resource.
- Block the worker or main thread.

### 3.3 `PricingCache` (`flow_proxy_plugin/utils/pricing_cache.py`)

A process-level singleton (held by `ProcessServices`) providing model pricing.

```python
class PricingCache:
    def get_price(self, model: str) -> tuple[float, float] | None:
        """Returns (input_cost_per_token, output_cost_per_token) or None."""
```

**Behaviour:**
- Lazy-loads pricing on first request for each model by calling `GET {target_base_url}/model/info`.
- Caches results per model name with a 1-hour TTL (wall clock, `time.time()`).
- Uses `threading.Lock` to protect the cache dict.
- On HTTP error or timeout: logs `WARNING`, returns `None`. Does not raise.
- `reset()` method for tests.

### 3.4 `StreamingState` additions

Two new fields added to the existing `StreamingState` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `usage_queue` | `queue.Queue[bytes \| None]` | Independent channel from worker to `UsageParser`. |
| `start_time` | `float` | `time.monotonic()` recorded at start of `handle_request()`. |
| `request_model` | `str` | Model name extracted from request body; empty string if absent. |

### 3.5 Log Format

One structured `INFO` log line emitted by `UsageParser` after stream completion, using the existing `set_request_context` mechanism (so `[req_id][WS]` prefix is present):

```
[abc123][WS] USAGE model=claude-3-5-sonnet config=prod-1 prompt_tokens=1024 completion_tokens=512 total_tokens=1536 cost_usd=0.003456 duration_ms=4231
```

Fields with `None` values are omitted from the log line (e.g. `cost_usd` omitted when pricing unavailable).

---

## 4. Failure Handling and Edge Cases

### 4.1 Missing `usage` in Response

When the client does not send `stream_options: {include_usage: true}` and LiteLLM is not configured with `always_include_stream_usage: true`, no usage chunk appears in the stream.

**Behaviour:** `UsageParser` logs the line with `prompt_tokens`, `completion_tokens`, `total_tokens` omitted, and `cost_usd` omitted. No exception raised.

### 4.2 `PricingCache` Failure

HTTP error, timeout, or malformed response from `/model/info`.

**Behaviour:** `WARNING` logged (once per failure, not per request), `get_price()` returns `None`, cost omitted from `UsageRecord`. Next request for the same model will retry.

### 4.3 `UsageParser` Thread Exception

Any unhandled exception inside `run()`.

**Behaviour:** `ERROR` logged with `exc_info=True`. Thread exits. No impact on main pipeline or worker.

### 4.4 Client Disconnect Before Stream Ends

`_reset_request_state()` is called, which cancels the worker. Worker's `finally` block still puts `None` into both `chunk_queue` and `usage_queue`. `UsageParser` receives the sentinel, records partial data (whatever it has), and exits cleanly.

If `UsageParser` is still running, `_reset_request_state()` joins it with `timeout=2.0s` (same as worker join timeout).

### 4.5 Stream Setup Failure

On pipe/`thread.start()` failure, `_streaming_state` is set to `None` and `clear_request_context()` is called. The `UsageParser` thread is never started, so no cleanup needed.

### 4.6 `PricingCache` Target URL

`PricingCache` receives `target_base_url` from `ProcessServices` (already available as `request_forwarder.target_base_url`). No new configuration required.

---

## 5. Integration Points

### 5.1 `ProcessServices` changes

- Add `pricing_cache: PricingCache` attribute, initialised in `_initialize()`.
- Add `pricing_cache` to `reset()` for test isolation.

### 5.2 `handle_request()` changes

- Extract `request_model` from request body before launching worker.
- Set `start_time = time.monotonic()`.
- Create `usage_queue = queue.Queue()`.
- Pass `usage_queue`, `start_time`, `request_model` to `StreamingState`.
- Launch `UsageParser` daemon thread alongside the existing worker thread.

### 5.3 `_streaming_worker()` changes

- After each `chunk_queue.put(chunk)` call: also call `usage_queue.put(chunk)`.
- In `finally`: also call `usage_queue.put(None)`.

### 5.4 `_reset_request_state()` changes

- Join `UsageParser` thread (if present) with `timeout=2.0s`.

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
| `test_parse_usage_from_sse_chunks` | Feed chunks with final usage chunk; assert correct token counts extracted. |
| `test_parse_model_from_first_chunk` | No `request_model`; first chunk has `"model"` field; assert extracted. |
| `test_request_model_takes_priority` | `request_model` set; response also has `"model"`; assert `request_model` used. |
| `test_missing_usage_graceful` | All chunks lack usage; assert `UsageRecord` has `None` tokens, no exception. |
| `test_unknown_model_no_cost` | model `"unknown"`, pricing unavailable; assert `cost_usd=None`, log line written. |
| `test_done_sentinel_ignored` | `data: [DONE]` line does not cause JSON parse error. |
| `test_parser_thread_exception_logged` | Inject malformed data; assert `ERROR` logged, no propagation. |

### 6.2 New: `tests/test_pricing_cache.py`

| Test | Assertion |
|------|-----------|
| `test_fetch_and_cache` | First call hits HTTP; second call for same model uses cache. |
| `test_ttl_expiry_triggers_refetch` | Artificially expire TTL; next call re-fetches. |
| `test_failure_returns_none` | `/model/info` returns 500; `get_price()` returns `None`, logs `WARNING`. |
| `test_thread_safe_concurrent_access` | Multiple threads call `get_price()` concurrently; no corruption. |
| `test_reset_clears_cache` | `reset()` clears all cached entries. |

### 6.3 Modified: `tests/test_web_server_plugin.py`

| Test | Change |
|------|--------|
| `test_streaming_state_defaults` | Assert `usage_queue` is a `Queue`, `start_time` is `float`, `request_model` is `str`. |
| `test_handle_request_launches_worker` | Assert `usage_queue` passed to worker; `UsageParser` thread started. |
| `test_reset_request_state_signals_usage_queue` | Assert `None` sent to `usage_queue` on client disconnect. |

### 6.4 Modified: `tests/test_process_services.py`

- Assert `pricing_cache` is initialised in `ProcessServices.get()`.
- Assert `pricing_cache.reset()` called in `ProcessServices.reset()`.

---

## 7. Backward Compatibility

- No configuration changes; no new environment variables.
- No changes to request or response wire format.
- `PricingCache` uses the existing `target_base_url` from `RequestForwarder`; no new secrets or endpoints required.
- All new fields in `StreamingState` have defaults (`request_model: str = ""`).

---

## References

- Streaming robustness design: `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md`
- B3 async streaming design: `docs/superpowers/specs/2026-03-13-async-streaming-design.md`
- LiteLLM streaming usage: `stream_options: {include_usage: true}` / proxy `always_include_stream_usage: true`
- LiteLLM pricing endpoint: `GET /model/info` → `input_cost_per_token`, `output_cost_per_token`
