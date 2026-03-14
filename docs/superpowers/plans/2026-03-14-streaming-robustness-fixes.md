# Streaming Robustness Fixes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复代码审查发现的 5 个问题：1 个失败测试、4 个需要补充的测试/代码，以及 1 个规范文档更新。

**Architecture:** 仅修改测试文件和 `web_server_plugin.py`，不实现 Phase 2 功能。所有修改均对齐设计规范 `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md`。

**Tech Stack:** Python 3.12+, pytest, unittest.mock

---

## Chunk 1: 修复测试失败 + 明确 fallback 测试

### Task 1: 修复 `test_send_error_default` 失败断言

**Files:**
- Modify: `tests/test_web_server_plugin.py:238`

当前代码：
```python
assert b"500 Error" in response_bytes          # line 238 — 断言旧行为
assert b"Internal server error" in response_bytes
```

实现后 `_send_error()` 使用 `_REASON_PHRASES` 返回 `"500 Internal Server Error"`，但测试仍在断言已废弃的 `b"500 Error"`，导致测试套件失败。

- [ ] **Step 1: 运行测试确认当前失败**

```bash
cd /Users/oscaner/Projects/flow-proxy
poetry run pytest tests/test_web_server_plugin.py::TestSendError::test_send_error_default -v
```

期望：**FAIL** — `AssertionError: assert b'500 Error' in ...`

- [ ] **Step 2: 修复断言**

将 `tests/test_web_server_plugin.py` 第 238 行：
```python
assert b"500 Error" in response_bytes
assert b"Internal server error" in response_bytes
```
改为：
```python
assert b"500 Internal Server Error" in response_bytes
assert b"Internal server error" in response_bytes
```

- [ ] **Step 3: 运行测试确认通过**

```bash
poetry run pytest tests/test_web_server_plugin.py::TestSendError::test_send_error_default -v
```

期望：**PASS**

---

### Task 2: 修复 `test_send_error_custom` 隐性依赖 fallback 路径

**Files:**
- Modify: `tests/test_web_server_plugin.py:251`

当前代码断言 `b"404 Error"`，实际上依赖了 `_REASON_PHRASES` 不包含 404 时的 fallback。如果将来 404 被加入 `_REASON_PHRASES`，测试会静默失败。需要明确该测试的意图：验证 fallback 路径行为。重命名方法并添加注释说明（断言内容不变，`mock_queue.assert_called_once()` 原测试已有，此处保留）。

- [ ] **Step 1: 修改测试明确 fallback 语义**

将 `tests/test_web_server_plugin.py` 第 241–252 行整个 `test_send_error_custom` 方法替换为：

```python
def test_send_error_custom_uses_fallback_for_unknown_code(
    self, plugin: FlowProxyWebServerPlugin
) -> None:
    """Status codes not in _REASON_PHRASES fall back to 'Error' as reason phrase.

    NOTE: 404 is intentionally NOT in _REASON_PHRASES; this test validates the
    fallback path. If 404 is ever added to _REASON_PHRASES, update this test
    to use a different uncovered code (e.g. 418).
    """
    mock_queue = Mock()
    plugin.client = Mock(queue=mock_queue)

    plugin._send_error(status_code=404, message="Not found")

    mock_queue.assert_called_once()
    call_args = mock_queue.call_args[0][0]
    response_bytes = bytes(call_args)
    assert b"404 Error" in response_bytes  # fallback reason phrase
    assert b"Not found" in response_bytes
```

- [ ] **Step 2: 运行测试确认通过**

```bash
poetry run pytest tests/test_web_server_plugin.py::TestSendError -v
```

期望：两个测试均 **PASS**

- [ ] **Step 3: 提交**

```bash
git add tests/test_web_server_plugin.py
git commit -m "fix(tests): update send_error assertions to match RFC 7231 reason phrases"
```

---

## Chunk 2: 补充超时截断测试

### Task 3: 为 `_resolve_runtime_config()` 添加边界值测试

**Files:**
- Modify: `tests/test_cli.py`

规范 §2.1 要求"范围外的值应被截断并记录警告"，但 `test_cli.py` 没有覆盖 `--client-timeout 0` 或 `--client-timeout 100000` 的截断路径，也没有验证警告是否被记录。`_resolve_runtime_config()` 现已作为独立函数可以直接测试，无需通过 `main()` 的全量路径。

- [ ] **Step 1: 在 `tests/test_cli.py` 文件顶部 import 区域添加 `import argparse`，然后在末尾添加新测试类**

首先确认 `tests/test_cli.py` 顶部 imports 块末尾（`import pytest` 之后）添加：

```python
import argparse
```

然后在 `TestCLI` 类之后追加以下新测试类。

> **注意：** `_make_args` 的返回类型注解 `argparse.Namespace` 在类定义时求值，必须在文件顶层 import `argparse`，否则会抛 `NameError`。
>
> `_make_args` 传 `num_workers=None` 会使 `_resolve_runtime_config` 内部调用 `multiprocessing.cpu_count()`，这是无副作用的系统调用，不影响超时相关断言，无需 mock。

```python
class TestResolveRuntimeConfig:
    """Unit tests for _resolve_runtime_config() — clamping and warning behaviour."""

    def _make_args(self, client_timeout: float, num_workers: int | None = None, no_threaded: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            client_timeout=client_timeout,
            num_workers=num_workers,
            no_threaded=no_threaded,
        )

    def test_timeout_below_min_clamped_to_1(self) -> None:
        """client_timeout=0 is clamped to 1 and a warning is logged."""
        from flow_proxy_plugin.cli import _resolve_runtime_config

        mock_logger = MagicMock()
        args = self._make_args(client_timeout=0)

        _, _, timeout = _resolve_runtime_config(args, mock_logger)

        assert timeout == 1
        mock_logger.warning.assert_called_once()
        warning_msg = str(mock_logger.warning.call_args)
        assert "clamped" in warning_msg.lower() or "0" in warning_msg

    def test_timeout_above_max_clamped_to_86400(self) -> None:
        """client_timeout=100000 is clamped to 86400 and a warning is logged."""
        from flow_proxy_plugin.cli import _resolve_runtime_config

        mock_logger = MagicMock()
        args = self._make_args(client_timeout=100000)

        _, _, timeout = _resolve_runtime_config(args, mock_logger)

        assert timeout == 86400
        mock_logger.warning.assert_called_once()
        warning_msg = str(mock_logger.warning.call_args)
        assert "clamped" in warning_msg.lower() or "86400" in warning_msg

    def test_timeout_in_range_no_warning(self) -> None:
        """client_timeout=300 is within range; no warning logged."""
        from flow_proxy_plugin.cli import _resolve_runtime_config

        mock_logger = MagicMock()
        args = self._make_args(client_timeout=300)

        _, _, timeout = _resolve_runtime_config(args, mock_logger)

        assert timeout == 300
        mock_logger.warning.assert_not_called()

    def test_timeout_boundary_min_no_warning(self) -> None:
        """client_timeout=1 is exactly at lower bound; no warning."""
        from flow_proxy_plugin.cli import _resolve_runtime_config

        mock_logger = MagicMock()
        args = self._make_args(client_timeout=1)

        _, _, timeout = _resolve_runtime_config(args, mock_logger)

        assert timeout == 1
        mock_logger.warning.assert_not_called()

    def test_timeout_boundary_max_no_warning(self) -> None:
        """client_timeout=86400 is exactly at upper bound; no warning."""
        from flow_proxy_plugin.cli import _resolve_runtime_config

        mock_logger = MagicMock()
        args = self._make_args(client_timeout=86400)

        _, _, timeout = _resolve_runtime_config(args, mock_logger)

        assert timeout == 86400
        mock_logger.warning.assert_not_called()
```

- [ ] **Step 2: 运行新测试确认通过**

```bash
poetry run pytest tests/test_cli.py::TestResolveRuntimeConfig -v
```

期望：5 个测试均 **PASS**

- [ ] **Step 3: 运行全部 CLI 测试确认无回归**

```bash
poetry run pytest tests/test_cli.py -v
```

期望：全部 **PASS**

- [ ] **Step 4: 提交**

```bash
git add tests/test_cli.py
git commit -m "test(cli): add clamping and warning tests for _resolve_runtime_config"
```

---

## Chunk 3: 补充代码修复

### Task 4: 在 `_finish_stream()` 添加 metrics 扩展点注释桩

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:189`

规范 §4.3 明确要求在 `_finish_stream()` 末尾添加以下注释桩（Phase 1 最低交付物）：

```python
# metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)
```

- [ ] **Step 1: 在 `_finish_stream()` 末尾（`clear_request_context()` 之后）添加注释桩**

`flow_proxy_plugin/plugins/web_server_plugin.py` 第 189 行末尾，`clear_request_context()` 之后插入：

```python
        clear_request_context()
        # metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)
```

- [ ] **Step 2: 运行相关测试确认无回归**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

期望：全部 **PASS**

---

### Task 5: 在 stream setup 异常块添加日志记录

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py:275`

规范 §4.1 将 "Stream setup failure" 列为 ERROR 级别日志事件。当前 `handle_request()` 的 stream setup `except Exception` 块没有任何 `self.logger.error()` 调用，异常被静默吞没，与 auth 失败路径不一致。

- [ ] **Step 1: 在 except 块中添加日志调用**

将 `flow_proxy_plugin/plugins/web_server_plugin.py` 第 275–287 行：

```python
        except Exception:
            try:
                os.close(pipe_r)
```

改为：

```python
        except Exception as setup_exc:
            self.logger.error("Failed to start streaming: %s", setup_exc, exc_info=True)
            try:
                os.close(pipe_r)
```

- [ ] **Step 2: 运行相关测试确认无回归**

```bash
poetry run pytest tests/test_web_server_plugin.py -v
```

期望：全部 **PASS**

- [ ] **Step 3: 提交（Tasks 4+5 一起）**

```bash
git add flow_proxy_plugin/plugins/web_server_plugin.py
git commit -m "fix(streaming): add metrics stub to _finish_stream and log setup exceptions"
```

---

## Chunk 4: 补充 `clear_request_context()` 调用断言

### Task 6: 在错误路径测试中断言 `clear_request_context()` 被调用

**Files:**
- Modify: `tests/test_web_server_plugin.py:802–862`

规范 §3.2 的 cleanup 不变量要求 `clear_request_context()` 在所有退出路径上被调用。现有的 auth 失败和 stream setup 失败测试仅验证了 `_streaming_state is None` 和 `client.queue.called`，没有直接断言 cleanup 不变量。

> **关于 Task 6 实际改动范围：**
> - `patch.object(ProcessServices, "get", return_value=mock_svc)` — 原测试（第 814–816 行）**已有**此 context manager，替换时**保留**。
> - `patch("flow_proxy_plugin.plugins.web_server_plugin.clear_request_context") as mock_clear` — **新增**，用于验证 cleanup 不变量。
> - `mock_clear.assert_called_once()` — **新增**，直接断言 `clear_request_context()` 恰好被调用一次。
> - 其余测试结构与原测试相同。

- [ ] **Step 1: 更新 `test_handle_request_cleans_up_pipe_on_auth_failure`**

将 `tests/test_web_server_plugin.py` 第 802–820 行的测试方法替换为：

```python
def test_handle_request_cleans_up_pipe_on_auth_failure(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
) -> None:
    """If auth fails, handle_request sends 503, clears context, and returns without leaking state."""
    mock_svc.load_balancer.get_next_config.side_effect = Exception("no config")

    request = Mock(spec=HttpParser)
    request.method = b"GET"
    request.path = b"/v1/test"
    request.headers = {}
    request.body = None
    request.buffer = None

    with (
        patch.object(ProcessServices, "get", return_value=mock_svc),
        patch(
            "flow_proxy_plugin.plugins.web_server_plugin.clear_request_context"
        ) as mock_clear,
    ):
        plugin.handle_request(request)

    assert plugin._streaming_state is None
    assert plugin.client.queue.called  # type: ignore[attr-defined]
    mock_clear.assert_called_once()  # cleanup invariant
```

- [ ] **Step 2: 更新 `test_handle_request_cleans_up_pipe_on_setup_failure`**

将 `tests/test_web_server_plugin.py` 第 822–862 行的测试方法替换为：

```python
def test_handle_request_cleans_up_pipe_on_setup_failure(
    self, plugin: FlowProxyWebServerPlugin, mock_svc: MagicMock
) -> None:
    """If threading.Thread.start() raises, pipe fds are closed, state is None, context cleared."""
    mock_svc.load_balancer.get_next_config.return_value = {
        "name": "cfg", "clientId": "cid", "clientSecret": "s", "tenant": "t"
    }
    mock_svc.jwt_generator.generate_token.return_value = "jwt-token"
    mock_svc.request_filter.find_matching_rule.return_value = None
    mock_svc.request_forwarder.target_base_url = "https://flow.ciandt.com"

    request = Mock(spec=HttpParser)
    request.method = b"GET"
    request.path = b"/v1/test"
    request.headers = {}
    request.body = None
    request.buffer = None

    import os
    captured_fds: list[int] = []
    real_pipe = os.pipe

    def capturing_pipe() -> tuple[int, int]:
        r, w = real_pipe()
        captured_fds.extend([r, w])
        return r, w

    with (
        patch("flow_proxy_plugin.plugins.web_server_plugin.os.pipe", side_effect=capturing_pipe),
        patch("threading.Thread.start", side_effect=RuntimeError("thread start failed")),
        patch.object(ProcessServices, "get", return_value=mock_svc),
        patch(
            "flow_proxy_plugin.plugins.web_server_plugin.clear_request_context"
        ) as mock_clear,
    ):
        plugin.handle_request(request)

    assert plugin._streaming_state is None
    assert plugin.client.queue.called  # type: ignore[attr-defined]
    mock_clear.assert_called_once()  # cleanup invariant

    # Both pipe fds must have been closed — re-closing should raise OSError
    assert len(captured_fds) == 2
    with pytest.raises(OSError):
        os.close(captured_fds[0])
    with pytest.raises(OSError):
        os.close(captured_fds[1])
```

- [ ] **Step 3: 运行更新的测试确认通过**

```bash
poetry run pytest tests/test_web_server_plugin.py::TestHandleRequest::test_handle_request_cleans_up_pipe_on_auth_failure tests/test_web_server_plugin.py::TestHandleRequest::test_handle_request_cleans_up_pipe_on_setup_failure -v
```

期望：两个测试均 **PASS**

- [ ] **Step 4: 运行全量测试确认无回归**

```bash
poetry run pytest tests/ -v
```

期望：全部 **PASS**

- [ ] **Step 5: 提交**

```bash
git add tests/test_web_server_plugin.py
git commit -m "test(streaming): assert clear_request_context called on all error exit paths"
```

---

## Chunk 5: 更新规范文档

### Task 7: 在规范文档中标记所有问题已修复

**Files:**
- Modify: `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md`

将 §5.1 "Fixes Already Shipped" 列表更新，加入本次修复的条目：

- [ ] **Step 1: 更新 §4.1 日志事件表格中 "Stream setup failure" 条目**

将 `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md` §4.1 表格中的：

```
| Stream setup failure | main | ERROR | Existing 500 path logs. |
```

替换为：

```
| Stream setup failure | main | ERROR | `"Failed to start streaming: {e}"` with `exc_info=True` |
```

（Task 5 新增了这条日志，原 "Existing 500 path logs." 描述的是不存在的日志，需更正。）

- [ ] **Step 2: 在 §5.1 末尾追加以下内容**

在 `docs/superpowers/specs/2026-03-13-streaming-robustness-design.md` 的 §5.1 最后一条（`clear_request_context()` on all exit paths）之后添加：

```markdown
- **Stream setup exception logging**
  `handle_request()` stream setup `except Exception` block now logs `ERROR` with `exc_info=True` (`"Failed to start streaming: {e}"`), consistent with auth-failure path and §4.1 log event table.
- **Metrics extension point stub**
  `_finish_stream()` now contains `# metrics hook (Phase 2): on_stream_finished(req_id, config_name, status_code, error)` as specified in §4.3.
- **Test suite correctness**
  `test_send_error_default` updated to assert `b"500 Internal Server Error"` (RFC 7231). `test_send_error_custom` renamed and documented to explicitly test the fallback path for status codes not in `_REASON_PHRASES`.
- **Clamping tests**
  `TestResolveRuntimeConfig` added to `test_cli.py`: covers below-min (0→1), above-max (100000→86400), in-range, and exact-boundary cases; verifies warning logged only when clamped.
- **Cleanup invariant tests**
  `test_handle_request_cleans_up_pipe_on_auth_failure` and `test_handle_request_cleans_up_pipe_on_setup_failure` updated to assert `clear_request_context()` is called, directly validating the §3.2 cleanup invariant.
```

同时，修复 §5.2 的列表编号错误（原文最后两项重复编号为 `4`，应为 `5` 和 `6`，已在第一轮修复中应用）。

- [ ] **Step 3: 提交**

```bash
git add docs/superpowers/specs/2026-03-13-streaming-robustness-design.md
git commit -m "docs: mark all Phase 1 code-review fixes as implemented in robustness spec"
```

---

## 执行验证

完成所有任务后，运行完整测试套件：

```bash
poetry run pytest tests/ -v --tb=short
```

期望：全部 **PASS**，无任何 `FAIL` 或 `ERROR`。
