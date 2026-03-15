# Transport Error Classification Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove unnecessary `mark_http_client_dirty()` calls from the streaming worker and classify `TransportError` subtypes into distinct `end=` log values for observability.

**Architecture:** Add `end_reason: str = ""` to `StreamingState`; replace the single `except httpx.TransportError` block with three ordered except clauses (`RemoteProtocolError`, `ConnectError`, generic `TransportError`) that each set `end_reason` without calling `mark_http_client_dirty()`; update `_finish_stream()` to use `state.end_reason` instead of the hardcoded `"transport_error"` string.

**Tech Stack:** Python 3.12, httpx, pytest

---

## Chunk 1: StreamingState field + worker exception handling + _finish_stream update

### Task 1: Add `end_reason` field to `StreamingState`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:67-72` (the defaulted-fields block of `StreamingState`)

- [ ] **Step 1: Open `flow_proxy_plugin/plugins/web_server_plugin.py` and locate the `StreamingState` dataclass**

  The dataclass starts at line 45. The defaulted-fields block currently ends at line 72:
  ```python
  headers_sent: bool = False  # guards error-response logic
  is_sse: bool = False  # set by read_from_descriptors when _ResponseHeaders processed
  status_code: int = 0  # stored after _ResponseHeaders item is processed
  error: BaseException | None = None  # set by worker on exception
  ttfb: float | None = None  # set by worker on first chunk/line
  bytes_sent: int = 0  # incremented in read_from_descriptors() main thread only
  ```

- [ ] **Step 2: Append `end_reason` after `bytes_sent`**

  Add the following line after `bytes_sent: int = 0`:
  ```python
  end_reason: str = ""  # written by worker before sentinel (same GIL invariant as error/ttfb); only valid when error is httpx.TransportError
  ```

  Also update the `StreamingState` docstring to document `end_reason`. Add after the `state.bytes_sent` sentence:
  ```
  state.end_reason is written by worker before queue.put(None) — same GIL guarantee
  as state.error; only valid when state.error is an httpx.TransportError.
  ```

- [ ] **Step 3: Update `test_streaming_state_defaults` to assert `end_reason == ""`**

  Locate the `test_streaming_state_defaults` test in `tests/test_web_server_plugin.py` (in the `TestDataStructures` class). Add an assertion for the new field:
  ```python
  assert state.end_reason == ""
  ```

- [ ] **Step 4: Run mypy to verify no type errors**

  ```bash
  poetry run mypy flow_proxy_plugin/plugins/web_server_plugin.py
  ```
  Expected: `Success: no issues found`

---

### Task 2: Replace single `except httpx.TransportError` with three-layer except

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:567-574` (the exception handling block in `_streaming_worker`)

- [ ] **Step 1: Write a failing test for `RemoteProtocolError` → no dirty call**

  In `tests/test_web_server_plugin.py`, inside the `TestStreamingWorker` class, add:

  ```python
  def test_worker_remote_protocol_error_no_dirty(
      self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
  ) -> None:
      """RemoteProtocolError sets end_reason='remote_closed' and does NOT mark client dirty."""
      import os
      state = self._make_state()
      mock_svc.http_client.stream.side_effect = httpx.RemoteProtocolError("peer closed")

      with patch.object(ProcessServices, "get", return_value=mock_svc):
          plugin._streaming_worker("GET", "https://example.com", {}, None, state)

      assert isinstance(state.error, httpx.RemoteProtocolError)
      assert state.end_reason == "remote_closed"
      assert state.chunk_queue.get_nowait() is None  # sentinel present
      mock_svc.mark_http_client_dirty.assert_not_called()
      os.close(state.pipe_r)
      os.close(state.pipe_w)
  ```

- [ ] **Step 2: Run the new test to confirm it fails**

  ```bash
  poetry run pytest tests/test_web_server_plugin.py::TestStreamingWorker::test_worker_remote_protocol_error_no_dirty -v
  ```
  Expected: FAIL — `assert state.end_reason == "remote_closed"` fails (field doesn't exist yet / `mark_http_client_dirty` is still called)

- [ ] **Step 3: Write a failing test for `ConnectError`**

  ```python
  def test_worker_connect_error_no_dirty(
      self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
  ) -> None:
      """ConnectError sets end_reason='connect_error' and does NOT mark client dirty."""
      import os
      state = self._make_state()
      mock_svc.http_client.stream.side_effect = httpx.ConnectError("refused")

      with patch.object(ProcessServices, "get", return_value=mock_svc):
          plugin._streaming_worker("GET", "https://example.com", {}, None, state)

      assert isinstance(state.error, httpx.ConnectError)
      assert state.end_reason == "connect_error"
      assert state.chunk_queue.get_nowait() is None
      mock_svc.mark_http_client_dirty.assert_not_called()
      os.close(state.pipe_r)
      os.close(state.pipe_w)
  ```

- [ ] **Step 4: Write a failing test for generic `TransportError`**

  ```python
  def test_worker_transport_error_no_dirty(
      self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
  ) -> None:
      """Generic TransportError sets end_reason='transport_error' and does NOT mark client dirty."""
      import os
      state = self._make_state()
      mock_svc.http_client.stream.side_effect = httpx.TransportError("conn failed")

      with patch.object(ProcessServices, "get", return_value=mock_svc):
          plugin._streaming_worker("GET", "https://example.com", {}, None, state)

      assert isinstance(state.error, httpx.TransportError)
      assert state.end_reason == "transport_error"
      assert state.chunk_queue.get_nowait() is None
      mock_svc.mark_http_client_dirty.assert_not_called()
      os.close(state.pipe_r)
      os.close(state.pipe_w)
  ```

- [ ] **Step 5: Run all three new tests to confirm they all fail**

  ```bash
  poetry run pytest tests/test_web_server_plugin.py -k "no_dirty" -v
  ```
  Expected: 3 FAILs

- [ ] **Step 6: Update the existing `test_worker_transport_error_sets_state_error_and_sentinel` test**

  This test (line ~570) currently asserts `mark_http_client_dirty.assert_called_once()`. Change that assertion to `assert_not_called()` and add `assert state.end_reason == "transport_error"`:

  Before:
  ```python
  assert isinstance(state.error, httpx.TransportError)
  assert state.chunk_queue.get_nowait() is None  # sentinel
  mock_svc.mark_http_client_dirty.assert_called_once()
  ```

  After:
  ```python
  assert isinstance(state.error, httpx.TransportError)
  assert state.end_reason == "transport_error"
  assert state.chunk_queue.get_nowait() is None  # sentinel
  mock_svc.mark_http_client_dirty.assert_not_called()
  ```

  Also update the docstring from `"stores error, marks client dirty, puts sentinel"` to `"stores error, sets end_reason, puts sentinel; does NOT mark client dirty"`.

- [ ] **Step 6b: Run the updated test to confirm it now fails**

  ```bash
  poetry run pytest tests/test_web_server_plugin.py::TestStreamingWorker::test_worker_transport_error_sets_state_error_and_sentinel -v
  ```
  Expected: FAIL — two assertion errors: `end_reason == "transport_error"` (field is `""`) and `mark_http_client_dirty.assert_not_called()` (still being called)

- [ ] **Step 7: Implement the three-layer except in `_streaming_worker`**

  Locate the current exception block in `_streaming_worker` (~line 567):
  ```python
  except httpx.TransportError as e:
      self.logger.warning("Transport error — marking httpx client dirty: %s", e)
      ProcessServices.get().mark_http_client_dirty()
      state.error = e
  ```

  Replace with:
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

  **Important:** `RemoteProtocolError` and `ConnectError` are subclasses of `TransportError` and must come before the base class.

- [ ] **Step 8: Run all four transport-error worker tests**

  ```bash
  poetry run pytest tests/test_web_server_plugin.py -k "transport_error or no_dirty or connect_error or remote_protocol" -v
  ```
  Expected: all PASS

- [ ] **Step 9: Run the full test suite to check for regressions**

  ```bash
  poetry run pytest -v
  ```
  Expected: all PASS (or pre-existing failures only)

---

### Task 3: Update `_finish_stream()` to use `state.end_reason`

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py` (~line 199 after Task 1 adds `end_reason`; the `end` assignment in `_finish_stream`)

  > Note: adding `end_reason` in Task 1 shifts all subsequent line numbers by one. Use your editor's search to locate `end = "transport_error"` rather than relying on the line number.

- [ ] **Step 1: Write failing tests for `_finish_stream()` log values**

  In `tests/test_web_server_plugin.py`, inside the **`TestEventLoopHooks`** class (which owns `_make_state_with_pipe()`), add three tests:

  ```python
  def test_finish_stream_end_reason_remote_closed(
      self, plugin: FlowProxyWebServerPlugin
  ) -> None:
      """_finish_stream logs end=remote_closed when state.end_reason='remote_closed'."""
      import time

      state, pipe_r, pipe_w = self._make_state_with_pipe()
      state.start_time = time.time() - 10.0
      state.stream = True
      state.ttfb = 3.9
      state.bytes_sent = 2657
      state.status_code = 200
      state.headers_sent = True
      state.is_sse = False
      state.error = httpx.RemoteProtocolError("peer closed")
      state.end_reason = "remote_closed"
      plugin._streaming_state = state
      plugin.client = MagicMock()
      plugin.logger = MagicMock()

      plugin._finish_stream(state)

      warning_calls = plugin.logger.warning.call_args_list
      completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
      assert len(completion) == 1
      msg = completion[0].args[0] % completion[0].args[1:]
      assert "end=remote_closed" in msg
      # _finish_stream closes both fds; re-closing must raise OSError
      with pytest.raises(OSError):
          os.close(pipe_r)
      with pytest.raises(OSError):
          os.close(pipe_w)

  def test_finish_stream_end_reason_connect_error(
      self, plugin: FlowProxyWebServerPlugin
  ) -> None:
      """_finish_stream logs end=connect_error when state.end_reason='connect_error'."""
      import time

      state, pipe_r, pipe_w = self._make_state_with_pipe()
      state.start_time = time.time() - 1.0
      state.stream = True
      state.ttfb = None
      state.bytes_sent = 0
      state.status_code = 0
      state.headers_sent = False
      state.is_sse = False
      state.error = httpx.ConnectError("refused")
      state.end_reason = "connect_error"
      plugin._streaming_state = state
      plugin.client = MagicMock()
      plugin.logger = MagicMock()

      plugin._finish_stream(state)

      warning_calls = plugin.logger.warning.call_args_list
      completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
      assert len(completion) == 1
      msg = completion[0].args[0] % completion[0].args[1:]
      assert "end=connect_error" in msg
      with pytest.raises(OSError):
          os.close(pipe_r)
      with pytest.raises(OSError):
          os.close(pipe_w)

  def test_finish_stream_end_reason_transport_error(
      self, plugin: FlowProxyWebServerPlugin
  ) -> None:
      """_finish_stream logs end=transport_error when state.end_reason='transport_error' (generic fallback)."""
      import time

      state, pipe_r, pipe_w = self._make_state_with_pipe()
      state.start_time = time.time() - 5.0
      state.stream = True
      state.ttfb = 1.2
      state.bytes_sent = 1024
      state.status_code = 200
      state.headers_sent = True
      state.is_sse = False
      state.error = httpx.TransportError("unknown")
      state.end_reason = "transport_error"
      plugin._streaming_state = state
      plugin.client = MagicMock()
      plugin.logger = MagicMock()

      plugin._finish_stream(state)

      warning_calls = plugin.logger.warning.call_args_list
      completion = [c for c in warning_calls if c.args and str(c.args[0]).startswith("← ")]
      assert len(completion) == 1
      msg = completion[0].args[0] % completion[0].args[1:]
      assert "end=transport_error" in msg
      with pytest.raises(OSError):
          os.close(pipe_r)
      with pytest.raises(OSError):
          os.close(pipe_w)
  ```

  > Note: `os` and `pytest` are already imported at module level in the test file; no local import needed.

- [ ] **Step 2: Also update `test_finish_stream_transport_error_emits_warning`**

  This existing test (line ~1108) sets `state.error = httpx.RemoteProtocolError("peer closed")` but asserts `"end=transport_error"`. After this change it should assert `"end=remote_closed"` because `_finish_stream` will use `state.end_reason`. Update the test:

  1. Set `state.end_reason = "remote_closed"` before calling `_finish_stream`
  2. Change assertion from `assert "end=transport_error" in msg` to `assert "end=remote_closed" in msg`
  3. Update the docstring from `"_finish_stream emits WARNING with end=transport_error when state.error is TransportError"` to `"_finish_stream emits WARNING with end=remote_closed when state.end_reason='remote_closed'"`

- [ ] **Step 3: Run the four `_finish_stream` tests to confirm they fail**

  ```bash
  poetry run pytest tests/test_web_server_plugin.py -k "finish_stream" -v
  ```
  Expected: `test_finish_stream_end_reason_*` FAILs, `test_finish_stream_transport_error_emits_warning` also FAILs (asserts `"end=remote_closed"` but gets `"end=transport_error"` from old code)

- [ ] **Step 4: Update `_finish_stream()` to use `state.end_reason`**

  Locate lines ~198-201 in `web_server_plugin.py`:
  ```python
  if isinstance(state.error, httpx.TransportError):
      end = "transport_error"
  else:
      end = "worker_error"
  ```

  Replace with:
  ```python
  if isinstance(state.error, httpx.TransportError):
      end = state.end_reason  # "remote_closed" | "connect_error" | "transport_error"
  else:
      end = "worker_error"
  ```

- [ ] **Step 5: Run all `_finish_stream` tests**

  ```bash
  poetry run pytest tests/test_web_server_plugin.py -k "finish_stream" -v
  ```
  Expected: all PASS

- [ ] **Step 6: Run the full test suite**

  ```bash
  poetry run pytest -v
  ```
  Expected: all PASS

- [ ] **Step 7: Run linters**

  ```bash
  poetry run ruff check flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
  poetry run mypy flow_proxy_plugin/plugins/web_server_plugin.py
  ```
  Expected: no errors

- [ ] **Step 8: Commit**

  ```bash
  git add flow_proxy_plugin/plugins/web_server_plugin.py tests/test_web_server_plugin.py
  git commit -m "fix: classify TransportError subtypes; remove mark_http_client_dirty from streaming worker"
  ```

---

## Chunk 2: Verification

### Task 4: Final verification

- [ ] **Step 1: Run the full test suite with coverage**

  ```bash
  poetry run pytest --cov=flow_proxy_plugin --cov-report=term-missing -v
  ```
  Expected: all tests pass; no new uncovered lines in `web_server_plugin.py`

- [ ] **Step 2: Run all linters**

  ```bash
  make lint
  ```
  Expected: ruff, mypy, pylint all pass

- [ ] **Step 3: Verify log output manually (optional smoke test)**

  If the service is running locally, trigger a streaming request and confirm the log shows `end=remote_closed` (or `end=connect_error`) instead of `end=transport_error` for backend-initiated closes.
