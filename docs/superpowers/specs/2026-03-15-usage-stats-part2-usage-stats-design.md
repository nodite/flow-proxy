# Usage Statistics — Part 2: UsageStats Persistence

**Date:** 2026-03-15
**Status:** Draft
**Component:** Flow Proxy Plugin — Usage Statistics (Part 2 of 3)
**Prerequisite:** Part 1 (UsageParser + PricingCache) must be complete.
**Next:** Part 3 (ProcessServices integration + test fixes)

---

## 1. Goals and Scope

This spec covers the **in-memory stats aggregation and `stats.json` persistence layer**:

- New module: `flow_proxy_plugin/utils/usage_stats.py` — `UsageStats` class with `record()`, `record_stream_event()`, `flush()`, `reset()`, and `StatsFlushThread`.
- `stats.json` schema: version, time granularities, bucket structure.
- New test file: `tests/test_usage_stats.py`.

**Out of scope for Part 2:**
- `ProcessServices` additions (Part 3).
- `record_stream_event()` call sites in `web_server_plugin.py` (Part 3).
- `UsageParser` + `PricingCache` (Part 1).

---

## 2. Architecture

### 2.1 Two-Tier Design

1. **In-process tier** (`UsageStats`): `UsageParser` calls `usage_stats.record()` after building a `UsageRecord`. The main thread calls `usage_stats.record_stream_event()` at lifecycle events. Both are pure in-memory operations (dict increment under `threading.Lock`).
2. **Disk tier**: `StatsFlushThread` wakes every `flush_interval` seconds and calls `usage_stats.flush(stats_file)`. Each process reads, merges its increments, and writes back with `fcntl.flock` to serialize multi-process writes.

**Why this handles high concurrency:**
- Hot path is lock-protected in-memory dict operations only — no I/O.
- Write frequency is decoupled from QPS; a burst of requests still results in one flush per interval.
- Multi-process safety: each flush is a read-increment-write cycle under an exclusive POSIX file lock.

**Data loss risk:** At most `flush_interval` seconds of data on hard crash. Acceptable for usage statistics.

---

## 3. `stats.json` Schema

**Location:** `~/.flow-proxy/stats.json` (parent directory created on first flush).

```json
{
  "version": 1,
  "last_flushed_at": "2026-03-15T12:00:00",
  "all_time": {
    "stream_requests": 1234,
    "stream_responses": 1100,
    "stream_errors": 134,
    "responses_by_status_class": {"2xx": 980, "4xx": 100, "5xx": 20},
    "errors_by_reason": {
      "auth_error": 10, "setup_failed": 5,
      "transport_error": 80, "worker_error": 30, "client_disconnect": 9
    },
    "ttfb_ms_sum": 250000.0,
    "ttfb_ms_count": 1100,
    "duration_ms_sum": 5000000.0,
    "duration_ms_count": 1234,
    "total_prompt_tokens": 1000000,
    "total_completion_tokens": 500000,
    "total_tokens": 1500000,
    "total_cost_usd": 45.67,
    "by_model": {
      "claude-3-5-sonnet": {
        "total_prompt_tokens": 700000,
        "total_completion_tokens": 350000,
        "total_tokens": 1050000,
        "total_cost_usd": 30.0
      }
    },
    "by_config": {
      "prod-1": {
        "stream_requests": 600,
        "stream_responses": 540,
        "stream_errors": 60,
        "total_prompt_tokens": 500000,
        "total_completion_tokens": 250000,
        "total_tokens": 750000,
        "total_cost_usd": 22.5
      }
    }
  },
  "daily": {
    "2026-03-15": { "... same bucket structure ..." }
  },
  "hourly": {
    "2026-03-15T12": { "... same bucket structure ..." }
  }
}
```

### 3.1 Bucket Structure

All three time granularities (`all_time`, each `daily` entry, each `hourly` entry) use the identical bucket structure.

| Field | Type | Incremented by | Notes |
|-------|------|----------------|-------|
| `stream_requests` | int | `record_stream_event("started")` | All requests where worker thread started. |
| `stream_responses` | int | `record_stream_event("response")` | Requests that received ≥1 chunk from backend. |
| `stream_errors` | int | `record_stream_event("error")` | Auth failure, setup failure, worker error, client disconnect. |
| `responses_by_status_class` | dict[str, int] | `record_stream_event("response")` | Keys: `"2xx"`, `"4xx"`, `"5xx"`. Status class computed as `f"{status_code // 100}xx"`. |
| `errors_by_reason` | dict[str, int] | `record_stream_event("error")` | Keys: `"auth_error"`, `"setup_failed"`, `"transport_error"`, `"worker_error"`, `"client_disconnect"`. |
| `ttfb_ms_sum` | float | `record_stream_event("response")` | Sum of TTFB values. `None` TTFB skipped. |
| `ttfb_ms_count` | int | `record_stream_event("response")` | Count of requests with non-None TTFB. |
| `duration_ms_sum` | float | `record_stream_event("response"/"error")` | Sum of all durations. |
| `duration_ms_count` | int | same | Count of requests with duration. |
| `total_prompt_tokens` | int | `record()` | Sum; `None` skipped. |
| `total_completion_tokens` | int | `record()` | Sum; `None` skipped. |
| `total_tokens` | int | `record()` | Sum; `None` skipped. |
| `total_cost_usd` | float | `record()` | Sum; `None` skipped. |
| `by_model` | dict[str, sub-bucket] | `record()` | Per-model breakdown. Sub-bucket: `total_prompt_tokens`, `total_completion_tokens`, `total_tokens`, `total_cost_usd` only. **No `stream_requests` in `by_model`** — model is not reliably known at `"started"` time. |
| `by_config` | dict[str, sub-bucket] | both | Per-config breakdown. Sub-bucket: `stream_requests`, `stream_responses`, `stream_errors`, `total_prompt_tokens`, `total_completion_tokens`, `total_tokens`, `total_cost_usd`. |

### 3.2 Retention Policy

- `all_time`: cumulative, never trimmed.
- `daily`: kept indefinitely.
- `hourly`: trimmed to the last 7 days (168 entries max) on each flush. Keys older than `now - 7 days` are removed.

---

## 4. `UsageStats` (`flow_proxy_plugin/utils/usage_stats.py`)

A process-level singleton instantiated by `ProcessServices` (Part 3) with:
```python
UsageStats(
    stats_file=Path.home() / ".flow-proxy" / "stats.json",
    flush_interval=int(os.getenv("FLOW_PROXY_STATS_FLUSH_INTERVAL", "300")),
)
```

`flush_interval` is clamped to the range `[10, 3600]`.

### 4.1 Public Interface

```python
from datetime import datetime
from pathlib import Path

class UsageStats:
    def __init__(self, stats_file: Path, flush_interval: int) -> None:
        """Initialize in-memory state and start StatsFlushThread."""

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
        event: str,                  # "started" | "response" | "error"
        status_code: int | None,     # for "response" event only
        error_reason: str | None,    # for "error" event only
        ttfb_ms: float | None,       # for "response" event; None if not measured
        duration_ms: float | None,   # for "response" and "error" events
        ts: datetime,
    ) -> None:
        """Stream lifecycle event. Thread-safe in-memory increment. No I/O."""

    def flush(self, stats_file: Path) -> None:
        """Merge in-memory increments into stats_file and reset counters."""

    def reset(self) -> None:
        """Clear in-memory counters and stop flush thread. For tests only."""
```

### 4.2 `record()` Internals

- Acquires `self._lock`.
- Updates three buckets: `_pending["all_time"]`, `_pending["daily"][ts.strftime("%Y-%m-%d")]`, `_pending["hourly"][ts.strftime("%Y-%m-%dT%H")]`.
- Increments only token/cost fields and `by_model`/`by_config` sub-bucket token/cost fields. **Does NOT touch `stream_requests`** — owned exclusively by `record_stream_event("started")`.
- Auto-vivication: if `by_config[config_name]` or `by_model[model]` sub-bucket does not yet exist, create it with all fields at zero.
- `None` values for token/cost fields are skipped (field not incremented).

### 4.3 `record_stream_event()` Internals

- Same lock and three-bucket update.
- **`config_name=""` handling**: when `config_name` is empty (auth failure path — no config was selected), update only the top-level bucket counters (`stream_requests`, `stream_errors`, etc.). Skip `by_config` entirely. An empty-string key in `by_config` is meaningless and would pollute stats. Concretely: all `by_config[config_name].*` updates are wrapped with `if config_name:`.
- Auto-vivication for `by_config[config_name]` (only when `config_name` is non-empty): if the sub-bucket does not exist, create it with all fields at zero before incrementing.
- `"started"`: increments top-level `stream_requests`. If `config_name` is non-empty, also increments `by_config[config_name].stream_requests`. Does not touch `by_model`.
- `"response"`: increments top-level `stream_responses`, `responses_by_status_class[status_class]`, adds to `ttfb_ms_sum`/`ttfb_ms_count` (only if `ttfb_ms` is not `None`), adds to `duration_ms_sum`/`duration_ms_count` (only if `duration_ms` is not `None`). If `config_name` is non-empty, also increments `by_config[config_name].stream_responses`.
- `"error"`: increments top-level `stream_errors`, `errors_by_reason[error_reason]`, adds to `duration_ms_sum`/`duration_ms_count` (only if `duration_ms` is not `None`). If `config_name` is non-empty, also increments `by_config[config_name].stream_errors`.

### 4.4 `flush()` Internals

1. Under `self._lock`, **atomic swap**: replace `self._pending` with a fresh empty dict. In-flight `record()` calls on other threads see the new empty dict immediately after the swap.
2. If the swapped snapshot is empty, return early (no I/O).
3. Create parent directory if absent (`stats_file.parent.mkdir(parents=True, exist_ok=True)`).
4. Open the dedicated lock file at `stats_file.with_suffix(".lock")` with `open(..., "a")` and hold it open for the entire flush operation. Acquire `fcntl.flock(fd, fcntl.LOCK_EX)`. The fd must remain open until step 9 — closing it before then releases the lock prematurely.
5. Read existing `stats.json`. If absent or corrupt (invalid JSON), start from an empty structure: `{"version": 1, "all_time": {}, "daily": {}, "hourly": {}}`.
6. Merge snapshot increments into the file's existing values bucket by bucket. For each numeric field: `existing_value + snapshot_value`. For nested dicts (`by_model`, `by_config`, `responses_by_status_class`, `errors_by_reason`): recurse.
7. Trim `hourly` keys: remove any key where the parsed hour is older than `datetime.now() - timedelta(days=7)`.
8. Update `last_flushed_at = datetime.now().isoformat(timespec="seconds")`.
9. **Atomic write (still under the lock)**: serialize merged data to `stats_file.with_suffix(".tmp")`, then `os.replace(tmp_path, stats_file)`. Both the write and the `os.replace()` must complete **before** releasing the lock, so that a second process acquiring the lock after step 9 always sees a fully written file. Multiple processes using the same `.tmp` path is safe because the exclusive lock serializes all flushers.
10. Release lock by closing the lock file fd.

### 4.5 `StatsFlushThread`

A daemon `threading.Thread` started by `UsageStats.__init__()`. Calls `flush(self._stats_file)` every `flush_interval` seconds using a `threading.Event` for clean shutdown. The thread exits when `reset()` sets the stop event or when the process exits (daemon thread).

```python
class StatsFlushThread(threading.Thread):
    def __init__(self, usage_stats: "UsageStats", flush_interval: int) -> None: ...
    def run(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            self._usage_stats.flush(self._usage_stats._stats_file)
    def stop(self) -> None:
        self._stop.set()
```

### 4.6 `reset()` Internals

Called from tests only (via `ProcessServices.reset()`):
1. Stop `StatsFlushThread` (call `stop()` and `join(timeout=2.0)`).
2. Under `self._lock`, clear `self._pending`.

---

## 5. `record_stream_event()` Call Sites

These call sites are implemented in **Part 3** (in `web_server_plugin.py`). Listed here for reference so `UsageStats` can be tested against them.

| Call site | Event | Key arguments |
|-----------|-------|---------------|
| `handle_request()` — after `state.thread.start()` | `"started"` | `config_name`, `ts=datetime.now()` |
| `handle_request()` — auth failure | `"error"` | `config_name=""`, `error_reason="auth_error"`, `duration_ms=(time.monotonic()-start_time)*1000`, `ts=datetime.now()` |
| `handle_request()` — setup failure | `"error"` | `config_name` (bound; auth succeeded), `error_reason="setup_failed"`, `duration_ms=(time.monotonic()-start_time)*1000`, `ts=datetime.now()` |
| `_finish_stream()` — `state.error is None` and `state.status_code != 0` | `"response"` | `config_name`, `status_code`, `ttfb_ms=state.ttfb * 1000 if state.ttfb is not None else None` (seconds→ms conversion — see §5.1), `duration_ms`, `ts` |
| `_finish_stream()` — `isinstance(state.error, httpx.TransportError)` | `"error"` | `config_name`, `error_reason="transport_error"`, `duration_ms`, `ts` |
| `_finish_stream()` — other `state.error` | `"error"` | `config_name`, `error_reason="worker_error"`, `duration_ms`, `ts` |
| `_reset_request_state()` — client disconnect | `"error"` | `config_name`, `error_reason="client_disconnect"`, `duration_ms=(time.monotonic()-state.start_time)*1000`, `ts=datetime.now()` |

### 5.1 `state.ttfb` Units

The existing `state.ttfb` field stores the TTFB value as **seconds** (e.g., `state.ttfb = time.monotonic() - state.start_time`). When passed to `record_stream_event(ttfb_ms=...)`, it must be converted: `ttfb_ms=state.ttfb * 1000 if state.ttfb is not None else None`.

This conversion is implemented in Part 3.

### 5.2 `status_code != 0` Guard

`record_stream_event("response", ...)` is only called when `state.status_code != 0`. This handles the edge case where the OSError early-return in `_streaming_worker()` fires before the `_ResponseHeaders` pipe-notification is written — `state.status_code` remains `0` (headers never delivered). The guard prevents a spurious `"response"` event.

---

## 6. Environment Variable

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `FLOW_PROXY_STATS_FLUSH_INTERVAL` | `300` | `10`–`3600` | Stats flush interval in seconds. |

---

## 7. Testing (`tests/test_usage_stats.py`)

All tests use a temporary directory for `stats_file` (via `tmp_path` pytest fixture). `ProcessServices.reset()` is called before and after each test to ensure clean state.

| Test | Assertion |
|------|-----------|
| `test_record_updates_all_buckets` | Single `record()` call; assert `_pending` has `all_time`, `daily`, `hourly` entries with correct token values. |
| `test_record_thread_safe` | 100 threads each call `record()` 10 times concurrently; assert `total_prompt_tokens` correctly summed (no race corruption). |
| `test_record_skips_none_tokens` | `record()` with `None` tokens; assert token fields are absent (or zero) in pending bucket — not corrupted. |
| `test_flush_creates_stats_file` | No `stats.json` present; `flush()` creates it with correct JSON structure including `"version": 1`. |
| `test_flush_merges_increments` | Existing `stats.json` with `stream_requests=5`; call `record_stream_event("started", ...)` 3 times then `flush()`; assert `stream_requests==8`. |
| `test_flush_resets_pending` | After `flush()`, `_pending` is empty; second `flush()` returns early (no file write — mock `os.replace` to confirm not called again). |
| `test_flush_atomic_write` | Mock `os.replace` to raise; assert original `stats.json` unchanged. |
| `test_flush_multiprocess_safe` | Two threads call `flush()` concurrently on the same file; assert no data corruption or lost increments (total = sum of all calls). |
| `test_hourly_trimmed_to_7_days` | Create `UsageStats` instance and call `record_stream_event` with 200 distinct hours injected into `_pending`; after `flush()`, only last 168 hourly keys remain. |
| `test_flush_interval_clamped` | `flush_interval=5` (below minimum); assert clamped to 10. `flush_interval=9999`; assert clamped to 3600. |
| `test_flush_thread_fires_on_interval` | **`@pytest.mark.slow`** — `StatsFlushThread` with `flush_interval=1`; call `record_stream_event("started")`; sleep 2 s; assert `stats.json` written. Mark `@pytest.mark.slow` so it can be excluded from fast CI runs. Do not use this test to verify flush logic correctness — use `test_flush_creates_stats_file` and `test_flush_merges_increments` (which call `flush()` directly) for that. |
| `test_record_stream_event_started` | `record_stream_event(event="started", config_name="c1", ts=...)`; assert `all_time.stream_requests==1`, `by_config["c1"].stream_requests==1`, no `stream_errors`. |
| `test_record_stream_event_response` | `record_stream_event(event="response", status_code=200, ttfb_ms=123.0, duration_ms=4000.0)`; assert `stream_responses==1`, `responses_by_status_class["2xx"]==1`, `ttfb_ms_sum==123.0`, `ttfb_ms_count==1`, `duration_ms_sum==4000.0`. |
| `test_record_stream_event_response_no_ttfb` | `ttfb_ms=None`; assert `ttfb_ms_count==0`, `ttfb_ms_sum==0.0`. |
| `test_record_stream_event_error_reasons` | One event per `error_reason` key; assert `stream_errors==5`, each key in `errors_by_reason==1`. |
| `test_record_stream_event_4xx_status_class` | `status_code=429`; assert `responses_by_status_class["4xx"]==1`. |
| `test_record_stream_event_5xx_status_class` | `status_code=503`; assert `responses_by_status_class["5xx"]==1`. |
| `test_by_model_no_stream_requests` | `record(model="m", ...)` then `flush()`; assert `by_model["m"]` in `stats.json` has no `stream_requests` key. |
| `test_by_config_all_fields` | Mix of `record_stream_event` and `record` for same `config_name`; assert `by_config` has `stream_requests`, `stream_responses`, `stream_errors`, and token fields all correctly summed. |
| `test_record_stream_event_empty_config_name_skips_by_config` | `record_stream_event(event="error", config_name="", error_reason="auth_error", ...)`; assert `stream_errors==1` in `all_time` top-level, but `all_time.by_config` does not contain the `""` key. |

---

## 8. Backward Compatibility

- No changes to environment variables (other than the new `FLOW_PROXY_STATS_FLUSH_INTERVAL`), request/response wire format, or existing public APIs.
- `~/.flow-proxy/` directory is created on first flush; no pre-existing configuration required.
- `LogCleaner` is **not** integrated with `UsageStats` — they are independent subsystems with incompatible process lifetimes (LogCleaner in parent, UsageStats in child processes).

---

## 9. References

- Original combined spec: `docs/superpowers/specs/2026-03-14-usage-stats-design.md`
- Part 1 (UsageParser + PricingCache): `docs/superpowers/specs/2026-03-15-usage-stats-part1-usage-parser-pricing-cache-design.md`
- Part 3 (ProcessServices + test fixes): `docs/superpowers/specs/2026-03-15-usage-stats-part3-integration-design.md`
