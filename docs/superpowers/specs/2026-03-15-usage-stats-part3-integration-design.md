# Usage Statistics — Part 3: ProcessServices Integration + Test Fixes

**Date:** 2026-03-15
**Status:** Draft
**Component:** Flow Proxy Plugin — Usage Statistics (Part 3 of 3)
**Prerequisite:** Part 1 (UsageParser + PricingCache) and Part 2 (UsageStats) must be complete.

---

## 1. Goals and Scope

This spec covers all **wiring and test updates** needed to make the usage stats system fully operational:

- `ProcessServices` additions: `pricing_cache` and `usage_stats` attributes.
- `handle_request()` additions: `record_stream_event("started")` after worker start, `record_stream_event("error")` on auth and setup failure. Remove the `hasattr` guards from Part 1.
- `_finish_stream()` additions: replace the `# metrics hook (Phase 2)` stub with `record_stream_event()` calls.
- `_reset_request_state()` additions: `record_stream_event("error", error_reason="client_disconnect")`.
- Update `tests/test_web_server_plugin.py`: add `usage_queue` to all `StreamingState` construction sites; add new assertions.
- Update `tests/test_process_services.py`: assert `pricing_cache` and `usage_stats` are initialized and reset correctly.

---

## 2. `ProcessServices` Changes (`flow_proxy_plugin/utils/process_services.py`)

### 2.1 New Imports

Add at the top of `process_services.py` (note: `process_services.py` lives inside `flow_proxy_plugin/utils/`, so same-package imports use `.`):

```python
from pathlib import Path
from .pricing_cache import PricingCache
from .usage_stats import UsageStats
```

### 2.2 New Attributes in `_initialize()`

Add after `self.request_forwarder` is set:

```python
self.pricing_cache = PricingCache(
    target_base_url=self.request_forwarder.target_base_url
)
```

Add anywhere after `self.request_forwarder` (order relative to other attributes is flexible):

```python
self.usage_stats = UsageStats(
    stats_file=Path.home() / ".flow-proxy" / "stats.json",
    flush_interval=int(os.getenv("FLOW_PROXY_STATS_FLUSH_INTERVAL", "300")),
)
```

### 2.3 Updates in `reset()`

The actual `reset()` method wraps `http_client.close()` in a `try/except Exception: pass` block:

```python
with cls._lock:
    if cls._instance is not None:
        try:
            if cls._instance.http_client is not None:
                cls._instance.http_client.close()
        except Exception:
            pass
    cls._instance = None
```

Add the new cleanup calls **after the entire `try/except` block** (not inside it), still inside `if cls._instance is not None:`, before `cls._instance = None`:

```python
with cls._lock:
    if cls._instance is not None:
        try:
            if cls._instance.http_client is not None:
                cls._instance.http_client.close()
        except Exception:
            pass
        # ADD HERE — after the try/except block, before cls._instance = None:
        if hasattr(cls._instance, "pricing_cache") and cls._instance.pricing_cache is not None:
            cls._instance.pricing_cache.reset()
        if hasattr(cls._instance, "usage_stats") and cls._instance.usage_stats is not None:
            cls._instance.usage_stats.reset()
    cls._instance = None
```

The `hasattr` guards are required because `_initialize()` may not have run if `reset()` is called between object construction and initialization during tests.

---

## 3. `web_server_plugin.py` Changes

### 3.0 Exception Safety of `record_stream_event()` Calls

All `record_stream_event()` calls added in §3.3–§3.5 are pure in-memory dict operations under a lock — they do not do any I/O and should not raise in normal operation. However, if `record_stream_event()` raises unexpectedly:

- In `_finish_stream()`: `clear_request_context()` (line 222) would be skipped, leaving the thread-local request context stale for the next request on that thread.
- In `_reset_request_state()`: same stale-context risk.
- In `handle_request()` after `state.thread.start()`: the worker thread is already running; an exception would propagate out of `handle_request()`, abandoning cleanup.

**Requirement**: wrap each `record_stream_event()` call in a `try/except Exception` with `logger.warning("Usage stats error: %s", e)` so that any failure is logged and execution continues normally. Example pattern:

```python
try:
    ProcessServices.get().usage_stats.record_stream_event(...)
except Exception as e:
    self.logger.warning("Usage stats error: %s", e)
```

Apply this pattern to all call sites listed in §3.3, §3.4, and §3.5.

### 3.1 New Imports

Add to the imports in `web_server_plugin.py` (note: `import time` already exists at the top of the file — do not add it again):

```python
from datetime import datetime
from ..utils.usage_parser import UsageParser
```

Remove the deferred `from ..utils.usage_parser import UsageParser` import that was placed inside `read_from_descriptors()` in Part 1 — move it to the module level here. (If Part 1 used module-level import already, no change needed.)

### 3.2 `usage_parser.py` Changes — Remove `hasattr` Guards

In `flow_proxy_plugin/utils/usage_parser.py` → `UsageParser.run()` (implemented in Part 1), remove the `hasattr(svc, "usage_stats")` and `hasattr(svc, "pricing_cache")` guards. Access `ProcessServices.get().usage_stats` and `ProcessServices.get().pricing_cache` directly.

### 3.3 `handle_request()` Changes

Four additions to `handle_request()`:

**1. Pre-initialize `config_name` before the auth `try` block:**

The existing code structure (after Part 1 changes) around the auth block is:

```python
req_id = secrets.token_hex(3)
set_request_context(req_id, "WS")
start_time = time.monotonic()       # changed from time.time() in Part 1
stream = self._parse_stream_field(request)
self.logger.info("→ %s %s stream=%s", method, path, stream)

# INSERT HERE: config_name = ""

try:
    _, config_name, jwt_token = self._get_config_and_token()
except Exception as e:
    self.logger.error("Auth failed: %s", e)
    self._send_error(503, "Auth error")
    # ADD record_stream_event call here (see item 2 below)
    clear_request_context()
    return
```

Add `config_name = ""` immediately before the `try:` block (between the `self.logger.info(...)` line and the `try:` line). This ensures `config_name` is always bound when the auth failure error path calls `record_stream_event(config_name=config_name, ...)`.

`start_time` is already initialized at this point via `time.monotonic()` (Part 1). No second assignment needed.

**2. Auth failure path** — after `self._send_error(503, "Auth error")` and before `clear_request_context()`:

```python
try:
    ProcessServices.get().usage_stats.record_stream_event(
        config_name=config_name,  # "" — no config was selected
        event="error",
        status_code=None,
        error_reason="auth_error",
        ttfb_ms=None,
        duration_ms=(time.monotonic() - start_time) * 1000,
        ts=datetime.now(),
    )
except Exception as e:
    self.logger.warning("Usage stats error: %s", e)
```

**3. Setup failure path** — after `self._send_error(500, "Failed to start streaming")` and before `clear_request_context()`:

```python
try:
    ProcessServices.get().usage_stats.record_stream_event(
        config_name=config_name,  # bound — auth succeeded
        event="error",
        status_code=None,
        error_reason="setup_failed",
        ttfb_ms=None,
        duration_ms=(time.monotonic() - start_time) * 1000,
        ts=datetime.now(),
    )
except Exception as e:
    self.logger.warning("Usage stats error: %s", e)
```

**4. After `state.thread.start()`** — record the "started" event:

```python
try:
    ProcessServices.get().usage_stats.record_stream_event(
        config_name=config_name,
        event="started",
        status_code=None,
        error_reason=None,
        ttfb_ms=None,
        duration_ms=None,
        ts=datetime.now(),
    )
except Exception as e:
    self.logger.warning("Usage stats error: %s", e)
```

### 3.4 `_finish_stream()` Changes

**Remove** the stub comment at line 223:
```python
# metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)
```

Insert the `record_stream_event()` calls **before** `clear_request_context()` at line 222 (not in place of the stub at line 223, which sits after `clear_request_context()`).

**Existing variable names to reuse** (from the actual `_finish_stream()` source):

| Existing variable | Set at | Use in `record_stream_event` |
|---|---|---|
| `end` | `"transport_error"` or `"worker_error"` (lines 198–201) | pass as `error_reason=end` |
| `duration` | `time.time() - state.start_time` (lines 204, 213) — **changed to `time.monotonic()` in Part 1 §4.1** | pass as `duration_ms=duration * 1000` |

Do NOT introduce a new `error_reason` variable — the existing `end` variable already serves this purpose.

The current `_finish_stream()` tail (condensed, with Part 1 changes already applied to `time.monotonic()`):

```python
if state.error:
    # ... error-response sending (unchanged) ...
    if isinstance(state.error, httpx.TransportError):
        end = "transport_error"
    else:
        end = "worker_error"
    status = ...
    ttfb_str = ...
    duration = time.monotonic() - state.start_time   # Part 1: changed from time.time()
    self.logger.warning("← %s ... end=%s", ..., end)
    # INSERT record_stream_event HERE (after the log, before clear_request_context)
else:
    status = ...
    ttfb_str = ...
    duration = time.monotonic() - state.start_time   # Part 1: changed from time.time()
    log_func(...)
    # INSERT record_stream_event HERE (after the log, before clear_request_context)
clear_request_context()   # line 222 — single call, always runs
# metrics hook stub at line 223 — REMOVE this line
```

**The new code inserts `record_stream_event()` inside each branch after the existing log call, before the shared `clear_request_context()`. There must be exactly one `clear_request_context()` call — do not duplicate it.**

Updated tail (showing only the insertions — all existing lines are unchanged except `time.time()` → `time.monotonic()` per Part 1):

```python
if state.error:
    # ... existing lines unchanged (but duration already uses time.monotonic() per Part 1) ...
    try:
        ProcessServices.get().usage_stats.record_stream_event(
            config_name=state.config_name,
            event="error",
            status_code=None,
            error_reason=end,        # reuse existing 'end' variable
            ttfb_ms=None,
            duration_ms=duration * 1000,   # reuse existing 'duration' variable
            ts=datetime.now(),
        )
    except Exception as e:
        self.logger.warning("Usage stats error: %s", e)
else:
    # ... existing lines unchanged ...
    if state.status_code != 0:
        try:
            ProcessServices.get().usage_stats.record_stream_event(
                config_name=state.config_name,
                event="response",
                status_code=state.status_code,
                error_reason=None,
                ttfb_ms=state.ttfb * 1000 if state.ttfb is not None else None,
                duration_ms=duration * 1000,   # reuse existing 'duration' variable
                ts=datetime.now(),
            )
        except Exception as e:
            self.logger.warning("Usage stats error: %s", e)
clear_request_context()   # exactly one call — always runs regardless of branch
# remove the "metrics hook (Phase 2)" stub comment that was here
```

**`state.status_code != 0` guard**: When the OSError early-return in `_streaming_worker()` fires before the `_ResponseHeaders` pipe-notification is written, `state.status_code` remains `0` (headers never delivered to the main thread). The guard prevents a spurious `"response"` event.

**`state.ttfb` units**: `state.ttfb` is stored in seconds (set as `time.monotonic() - state.start_time`). Convert to milliseconds for `record_stream_event()`.

**`clear_request_context()` placement**: always called regardless of which branch ran or whether `record_stream_event()` fired. The `if state.status_code != 0:` guard must not skip `clear_request_context()`.

### 3.5 `_reset_request_state()` Changes

`_reset_request_state()` already computes `duration = time.time() - state.start_time` at line 245 (changed to `time.monotonic()` by Part 1 §4.1). Add the `record_stream_event()` call **after the existing log line** and **before** `clear_request_context()`, reusing the existing `duration` variable:

```python
# ... existing log line at line ~246-250 (unchanged) ...
try:
    ProcessServices.get().usage_stats.record_stream_event(
        config_name=state.config_name,
        event="error",
        status_code=None,
        error_reason="client_disconnect",
        ttfb_ms=None,
        duration_ms=duration * 1000,   # reuse existing 'duration' variable (line 245)
        ts=datetime.now(),
    )
except Exception as e:
    self.logger.warning("Usage stats error: %s", e)
clear_request_context()
```

The `record_stream_event()` call must be before `clear_request_context()` so the `[req_id][WS]` log context is still active if any logging occurs inside `record_stream_event()`.

---

## 4. `tests/test_web_server_plugin.py` Updates

### 4.1 Add `usage_queue` to All `StreamingState` Construction Sites

There are currently **4 construction sites** in `tests/test_web_server_plugin.py` that require `usage_queue` (a new required field added in Part 1):

**Site 1** — `TestDataStructures.test_streaming_state_defaults` (approx. line 29):
```python
state = StreamingState(
    pipe_r=pipe_r,
    pipe_w=pipe_w,
    chunk_queue=queue.Queue(),
    thread=None,
    cancel=threading.Event(),
    req_id="abc123",
    config_name="test-config",
    start_time=0.0,
    stream=None,
    usage_queue=queue.Queue(),   # ADD
)
```

**Site 2** — `TestDataStructures.test_streaming_state_new_fields` (approx. line 57):
```python
state = StreamingState(
    pipe_r=pipe_r, pipe_w=pipe_w,
    chunk_queue=queue.Queue(),
    thread=None,
    cancel=threading.Event(),
    req_id="abc123",
    config_name="test-config",
    start_time=t,
    stream=True,
    usage_queue=queue.Queue(),   # ADD
)
```

**Site 3** — `TestStreamingWorker._make_state()` (approx. line 463):
```python
return StreamingState(
    pipe_r=pipe_r, pipe_w=pipe_w,
    chunk_queue=q.Queue(),
    thread=None,
    cancel=threading.Event(),
    req_id="test01",
    config_name="cfg",
    start_time=0.0,
    stream=None,
    usage_queue=q.Queue(),   # ADD
)
```

**Site 4** — `TestEventLoopHooks._make_state_with_pipe()` (approx. line 674):
```python
state = StreamingState(
    pipe_r=pipe_r, pipe_w=pipe_w,
    chunk_queue=q.Queue(),
    thread=None,
    cancel=threading.Event(),
    req_id="abc",
    config_name="cfg",
    start_time=0.0,
    stream=None,
    usage_queue=q.Queue(),   # ADD
)
```

Run `grep -n "StreamingState(" tests/test_web_server_plugin.py` before implementing to confirm all sites. There should be exactly these 4 test sites plus 1 production site in `handle_request()`.

### 4.2 Updated Assertions for Existing Tests

| Test | Change |
|------|--------|
| `test_streaming_state_defaults` | Add assertions: `state.usage_queue` is a `queue.Queue`, `state.request_model == ""`, `state.usage_parser_thread is None`. |
| `test_streaming_state_new_fields` | Rename `test_streaming_state_new_fields` → keep as-is; its `start_time=t` assertion is unchanged (clock is now monotonic but test uses an explicit float). |

### 4.2.5 Patch `UsageParser` in Existing `read_from_descriptors` Tests

As described in Part 1 §5.3, after the `read_from_descriptors()` change, any test that manually pre-fills `state.chunk_queue` with a `_ResponseHeaders` item and calls `read_from_descriptors()` will incidentally start a `UsageParser` thread that blocks indefinitely on `state.usage_queue.get()`. This causes join timeouts in `_reset_request_state()` (wasting 2 s per test).

The following existing tests must be updated to patch `UsageParser`:

```
TestEventLoopHooks — MUST be patched (enqueue _ResponseHeaders):
  - test_read_from_descriptors_queues_each_chunk         (line ~737)
  - test_read_from_descriptors_sends_headers_on_first_item  (line ~765)
  - test_read_from_descriptors_tracks_bytes_sent         (line ~794)
  - test_is_sse_propagated_to_state_when_response_headers_processed  (line ~937)

TestEventLoopHooks — safe, no patch needed (do NOT enqueue _ResponseHeaders):
  - test_read_from_descriptors_returns_true_on_sentinel  (only None sentinel — line ~808)
  - test_get_descriptors_empty_after_stream_finishes     (only None sentinel — line ~826)
  - test_read_from_descriptors_noop_when_pipe_not_in_readables  (only None sentinel)
  - test_worker_error_before_headers_sends_503           (only None sentinel)
  - test_worker_error_after_headers_does_not_send_error_response  (only None sentinel)
```

The instruction to `grep -n "_ResponseHeaders" tests/test_web_server_plugin.py` before implementing is the authoritative check — run it to catch any additional tests not listed here.

Run `grep -n "_ResponseHeaders" tests/test_web_server_plugin.py` to find all test methods that enqueue a `_ResponseHeaders` item into `chunk_queue`. Each such test needs the following patch added around the `read_from_descriptors()` call:

```python
with patch("flow_proxy_plugin.plugins.web_server_plugin.UsageParser") as mock_parser_cls:
    mock_parser_cls.return_value.run = MagicMock()  # no-op: does not block on usage_queue
    result = asyncio.run(plugin.read_from_descriptors([pipe_r]))
```

Tests that do **not** enqueue a `_ResponseHeaders` item (e.g. tests that only put `None` sentinel or raw bytes) do not need patching — the `UsageParser` launch only happens when `isinstance(item, _ResponseHeaders)` is `True`.

### 4.3 New Tests

Add these tests to `tests/test_web_server_plugin.py`:

| Test | Location | Assertion |
|------|----------|-----------|
| `test_worker_feeds_usage_queue` | `TestStreamingWorker` | After `_streaming_worker()` completes, assert `usage_queue` contains the same byte chunks as `chunk_queue` (excluding `_ResponseHeaders`) plus the `None` sentinel. |
| `test_worker_sentinel_in_usage_queue` | `TestStreamingWorker` | After worker completes normally, assert `usage_queue.get()` eventually returns `None`. |
| `test_usage_parser_thread_launched_on_headers` | `TestEventLoopHooks` | After `read_from_descriptors()` processes a `_ResponseHeaders` item, assert `state.usage_parser_thread` is not `None` and `is_alive()` or has finished. |
| `test_reset_state_joins_usage_parser_thread` | `TestEventLoopHooks` | After `_reset_request_state()`, assert `state.usage_parser_thread` was joined (mock `threading.Thread.join` to confirm call order: worker join before usage_parser join). |
| `test_handle_request_records_started` | `TestHandleRequest` (or similar) | Mock `usage_stats`; after a successful `handle_request()`, assert `record_stream_event` called with `event="started"` and correct `config_name`. |
| `test_handle_request_records_auth_error` | `TestHandleRequest` | Mock `usage_stats`; make `_get_config_and_token()` raise; assert `record_stream_event("error", error_reason="auth_error")` called before `clear_request_context()`. |
| `test_handle_request_records_setup_failure` | `TestHandleRequest` | Mock `usage_stats`; make `os.pipe()` raise or `thread.start()` raise; assert `record_stream_event("error", error_reason="setup_failed")` called. |
| `test_finish_stream_records_response` | `TestEventLoopHooks` | Mock `usage_stats`; set `state.status_code=200`, `state.error=None`; call `_finish_stream(state)`; assert `record_stream_event("response", status_code=200)` called. |
| `test_finish_stream_skips_response_when_status_zero` | `TestEventLoopHooks` | Mock `usage_stats`; set `state.status_code=0`, `state.error=None`; call `_finish_stream(state)`; assert `record_stream_event` **not** called with `event="response"`. |
| `test_finish_stream_records_transport_error` | `TestEventLoopHooks` | Mock `usage_stats`; set `state.error=httpx.TransportError(...)`; call `_finish_stream(state)`; assert `record_stream_event("error", error_reason="transport_error")`. |
| `test_finish_stream_records_worker_error` | `TestEventLoopHooks` | Mock `usage_stats`; set `state.error=RuntimeError("boom")`; call `_finish_stream(state)`; assert `record_stream_event("error", error_reason="worker_error")`. |
| `test_reset_request_state_records_client_disconnect` | `TestEventLoopHooks` | Mock `usage_stats`; call `_reset_request_state()`; assert `record_stream_event("error", error_reason="client_disconnect")` called before `clear_request_context()`. |
| `test_ttfb_converted_to_ms_in_response_event` | `TestEventLoopHooks` | Mock `usage_stats`; set `state.ttfb=1.5` (seconds); call `_finish_stream(state)` with no error and non-zero status; assert `record_stream_event` called with `ttfb_ms=1500.0`. |

---

## 5. `tests/test_process_services.py` Updates

Add the following assertions to the existing `ProcessServices` tests (or add new test methods):

| Test | Assertion |
|------|-----------|
| `test_process_services_initializes_pricing_cache` | `ProcessServices.get().pricing_cache` is not `None` and is a `PricingCache` instance. |
| `test_process_services_initializes_usage_stats` | `ProcessServices.get().usage_stats` is not `None` and is a `UsageStats` instance. |
| `test_process_services_reset_calls_pricing_cache_reset` | Mock `pricing_cache.reset`; call `ProcessServices.reset()`; assert `pricing_cache.reset()` was called. |
| `test_process_services_reset_calls_usage_stats_reset` | Mock `usage_stats.reset`; call `ProcessServices.reset()`; assert `usage_stats.reset()` was called. |

---

## 6. Verification Checklist

Before marking this spec complete, confirm:

1. `grep -rn "StreamingState(" flow_proxy_plugin/ tests/` shows exactly **5** sites (1 production in `flow_proxy_plugin/plugins/web_server_plugin.py` + 4 test sites in `tests/test_web_server_plugin.py`), all with `usage_queue=`.
2. `grep -n "time.time()" flow_proxy_plugin/plugins/web_server_plugin.py` returns no hits (all replaced with `time.monotonic()`).
3. `grep -rn "hasattr.*pricing_cache\|hasattr.*usage_stats" flow_proxy_plugin/` returns no hits (guards removed).
4. `grep -n "metrics hook (Phase 2)" flow_proxy_plugin/` returns no hits (stub replaced).
5. `make test` passes with no failures.
6. `make lint` passes.

---

## 7. Backward Compatibility

- `StreamingState.usage_queue` is a new required field. All construction sites are updated in §4.1.
- No changes to environment variables (other than `FLOW_PROXY_STATS_FLUSH_INTERVAL` from Part 2), wire format, or public API.
- The `start_time` clock change (`time.time()` → `time.monotonic()`) from Part 1 is already in place. The `duration=%.1fs` log format is unchanged.

---

## 8. References

- Original combined spec: `docs/superpowers/specs/2026-03-14-usage-stats-design.md`
- Part 1 (UsageParser + PricingCache): `docs/superpowers/specs/2026-03-15-usage-stats-part1-usage-parser-pricing-cache-design.md`
- Part 2 (UsageStats persistence): `docs/superpowers/specs/2026-03-15-usage-stats-part2-usage-stats-design.md`
