# SSE 实时流式传输（httpx 替换 requests）实施计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Reverse Proxy 模式的 HTTP 客户端从 `requests` 替换为 `httpx`，消除 SSE 流式响应的缓冲延迟和中断问题。

**Architecture:** 修改 `FlowProxyWebServerPlugin`，用 `httpx.Client().stream()` 上下文管理器替换 `requests.request(..., stream=True)`，使用 `iter_bytes()` 零缓冲透传响应体，并修复 SSE 响应头缺失问题。

**Tech Stack:** Python 3.12+, httpx ^0.28, proxy-py ^2, pytest

---

## Chunk 1: 依赖变更与基础迁移

> **执行顺序**：先执行 Task 1（依赖），然后执行 Chunk 2 的 Task 3 Steps 1–4（写测试/确认红灯），再回来执行 Task 2（生产代码），最后执行 Task 3 Steps 5–7（确认绿灯）。

### Task 1: 替换 pyproject.toml 依赖

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: 更新 pyproject.toml**

在 `[tool.poetry.dependencies]` 中用 `httpx` 替换 `requests`，在 `[tool.poetry.group.dev.dependencies]` 中移除 `types-requests`：

```toml
[tool.poetry.dependencies]
python = "^3.12"
proxy-py = "^2"
pyjwt = "^2"
httpx = "^0.28"
json5 = "^0"
nested-property = "^1"
```

dev 依赖中删除这一行：
```toml
types-requests = "^2"
```

- [ ] **Step 2: 安装新依赖**

```bash
poetry add httpx
poetry remove requests
poetry remove --group dev types-requests
```

预期输出：`httpx` 已添加，`requests` 和 `types-requests` 各自移除，`poetry.lock` 已更新。

- [ ] **Step 3: 确认安装**

```bash
poetry run python -c "import httpx; print(httpx.__version__)"
```

预期：打印出 httpx 版本号（如 `0.28.x`）。

---

### Task 2: 重写 `web_server_plugin.py` 核心逻辑

**Files:**
- Modify: `flow_proxy_plugin/plugins/web_server_plugin.py`

这是本次改动的主体。逐步替换，保持测试通过。

- [ ] **Step 1: 替换 import**

将文件顶部的：
```python
import requests
```
替换为：
```python
import httpx
```

同时将方法签名中所有 `requests.Response` 类型注解替换为 `httpx.Response`（涉及 `_send_response`、`_log_response_details`、`_send_response_headers`、`_stream_response_body`、`_log_request_details` 的 `body` 参数类型不变）。

- [ ] **Step 2: 重写 `_forward_request` 和 `handle_request`**

将 `_forward_request` 的返回值从 `requests.Response` 改为不再返回 response 对象，而是将整个流式转发逻辑内联到 `handle_request` 中，通过 `with httpx.Client() as client: with client.stream(...)` 包裹。

新的 `handle_request`：

```python
def handle_request(self, request: HttpParser) -> None:
    """Handle web server request."""
    method = self._decode_bytes(request.method) if request.method else "GET"
    path = self._decode_bytes(request.path) if request.path else "/"

    self.logger.info("→ %s %s", method, path)

    try:
        _, config_name, jwt_token = self._get_config_and_token()

        # Build request params
        filter_rule = self.request_filter.find_matching_rule(request, path)
        if filter_rule:
            path = self.request_filter.filter_query_params(
                path, filter_rule.query_params_to_remove
            )

        target_url = f"{self.request_forwarder.target_base_url}{path}"
        headers = self._build_headers(request, jwt_token, filter_rule)
        body = self._get_request_body(request, filter_rule)

        if self.logger.isEnabledFor(logging.DEBUG):
            self._log_request_details(method, path, target_url, headers, body)

        self.logger.info("Sending request to backend: %s", target_url)

        timeout = httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)

        with httpx.Client() as client:
            with client.stream(
                method=method,
                url=target_url,
                headers=headers,
                content=body,
                timeout=timeout,
            ) as response:
                self.logger.info(
                    "Backend response: %d %s, Transfer-Encoding: %s, Content-Length: %s",
                    response.status_code,
                    response.reason_phrase,
                    response.headers.get("transfer-encoding", "none"),
                    response.headers.get("content-length", "none"),
                )

                if self.logger.isEnabledFor(logging.DEBUG):
                    self._log_response_details(response)

                self._send_response_headers(response)
                self._stream_response_body(response)

                log_func = (
                    self.logger.info
                    if response.status_code < 400
                    else self.logger.warning
                )
                log_func(
                    "← %d %s [%s]",
                    response.status_code,
                    response.reason_phrase,
                    config_name,
                )

    except (BrokenPipeError, ConnectionResetError) as e:
        self.logger.debug("Client disconnected (%s)", type(e).__name__)
    except httpx.RemoteProtocolError as e:
        self.logger.error(
            "Backend streaming failed (RemoteProtocolError): %s",
            str(e),
            exc_info=True,
        )
    except Exception as e:
        self.logger.error("✗ Request failed: %s", str(e), exc_info=True)
        self._send_error()
```

删除 `_forward_request` 方法（逻辑已内联）。

- [ ] **Step 3: 重写 `_send_response_headers`**

接收 `httpx.Response`，修复 SSE 响应头：

```python
def _send_response_headers(self, response: httpx.Response) -> None:
    """Send HTTP status line and headers."""
    is_sse = "text/event-stream" in response.headers.get("content-type", "")

    # Status line
    status_line = f"HTTP/1.1 {response.status_code} {response.reason_phrase}\r\n"
    self.client.queue(memoryview(status_line.encode()))

    # Headers
    skip_headers = {"connection"}
    if not is_sse:
        skip_headers.add("transfer-encoding")

    for name, value in response.headers.items():
        if name.lower() not in skip_headers:
            self.client.queue(memoryview(f"{name}: {value}\r\n".encode()))

    # SSE-specific headers to prevent intermediate proxy buffering
    if is_sse:
        self.client.queue(memoryview(b"Cache-Control: no-cache\r\n"))
        self.client.queue(memoryview(b"X-Accel-Buffering: no\r\n"))

    # End of headers
    self.client.queue(memoryview(b"\r\n"))
```

- [ ] **Step 4: 重写 `_stream_response_body`**

使用 `httpx.Response` 和 `iter_bytes()` 零缓冲透传：

```python
def _stream_response_body(self, response: httpx.Response) -> tuple[int, int]:
    """Stream response body to client.

    Returns:
        Tuple of (bytes_sent, chunks_sent)
    """
    bytes_sent = 0
    chunks_sent = 0

    for chunk in response.iter_bytes():
        if not chunk:
            continue

        if chunks_sent == 0:
            self.logger.info(
                "Received first chunk from backend: %d bytes", len(chunk)
            )

        if not self._is_client_connected():
            self.logger.debug(
                "Client disconnected - stopping (sent %d bytes in %d chunks)",
                bytes_sent,
                chunks_sent,
            )
            break

        try:
            self.client.queue(memoryview(chunk))
            bytes_sent += len(chunk)
            chunks_sent += 1
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if isinstance(e, OSError) and e.errno != 32:
                self.logger.warning("OS error during streaming: %s", e)
            else:
                self.logger.debug(
                    "Client disconnected during streaming - sent %d bytes",
                    bytes_sent,
                )
            break

    if chunks_sent > 0:
        self.logger.debug(
            "Streaming completed: %d bytes in %d chunks", bytes_sent, chunks_sent
        )

    return bytes_sent, chunks_sent
```

注意：移除 `flush()` 调用，移除 `finally: response.close()`（httpx 上下文管理器自动处理）。

- [ ] **Step 5: 重写 `_log_response_details`**

更新类型注解，`response.reason` → `response.reason_phrase`：

```python
def _log_response_details(self, response: httpx.Response) -> None:
    """Log detailed response information in DEBUG mode."""
    self.logger.debug("Response: %d %s", response.status_code, response.reason_phrase)
    self.logger.debug("  Response Headers: %s", dict(response.headers))

    content_type = response.headers.get("content-type", "unknown")
    if "text/event-stream" in content_type:
        self.logger.debug("  Response Body: <SSE streaming response>")
    else:
        content_length = response.headers.get("content-length", "unknown")
        self.logger.debug(
            "  Response Body: Content-Type=%s, Content-Length=%s",
            content_type,
            content_length,
        )
```

- [ ] **Step 6: 删除已废弃的 `_send_response` 方法**

`_send_response` 方法的逻辑已拆分到 `handle_request`（头部日志）、`_send_response_headers`、`_stream_response_body` 中，删除原方法。

- [ ] **Step 7: 运行 linter 检查**

```bash
cd /Users/kang/Projects/flow-proxy && poetry run ruff check flow_proxy_plugin/plugins/web_server_plugin.py
```

预期：无错误。如有 import 顺序问题，运行 `poetry run ruff format flow_proxy_plugin/plugins/web_server_plugin.py`。

---

## Chunk 2: TDD — 先写测试，再实现

**TDD 原则**：先写新测试（会失败），再实现生产代码使其通过。

### Task 3: 重写 `test_web_server_plugin.py`

**Files:**
- Modify: `tests/test_web_server_plugin.py`

- [ ] **Step 1: 替换 import 和添加公共测试工具函数**

将文件顶部替换为：

```python
"""Unit tests for FlowProxyWebServerPlugin."""

from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import Mock, patch

import httpx
import pytest
from proxy.http.parser import HttpParser

from flow_proxy_plugin.plugins.web_server_plugin import FlowProxyWebServerPlugin
```

在 `plugin` fixture 之后（类定义之前）添加公共 mock 工具函数：

```python
def make_mock_httpx_response(
    status_code: int = 200,
    reason_phrase: str = "OK",
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
) -> Mock:
    """Create a mock httpx.Response for testing."""
    response = Mock(spec=httpx.Response)
    response.status_code = status_code
    response.reason_phrase = reason_phrase
    response.headers = httpx.Headers(headers or {"content-type": "application/json"})
    response.iter_bytes.return_value = iter(chunks or [])
    return response


@contextmanager
def mock_httpx_stream(
    plugin: "FlowProxyWebServerPlugin",
    status_code: int = 200,
    reason_phrase: str = "OK",
    content_type: str = "application/json",
    chunks: list[bytes] | None = None,
) -> Generator[Mock, None, None]:
    """Mock httpx.Client.stream and _get_config_and_token for handle_request tests.

    Patches both httpx.Client (so stream() works as a context manager) and
    _get_config_and_token (so JWT generation is bypassed and the happy path is
    actually exercised, not the exception fallback).
    """
    mock_response = Mock(spec=httpx.Response)
    mock_response.status_code = status_code
    mock_response.reason_phrase = reason_phrase
    mock_response.headers = httpx.Headers({"content-type": content_type})
    mock_response.iter_bytes.return_value = iter(chunks or [])
    mock_response.__enter__ = Mock(return_value=mock_response)
    mock_response.__exit__ = Mock(return_value=False)

    mock_client = Mock()
    mock_client.stream.return_value = mock_response
    mock_client.__enter__ = Mock(return_value=mock_client)
    mock_client.__exit__ = Mock(return_value=False)

    mock_token_result = ({"clientId": "test"}, "test-config", "test-jwt-token")

    with (
        patch("httpx.Client", return_value=mock_client),
        patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
    ):
        yield mock_response
```

- [ ] **Step 2: 删除 `TestSendResponse` 测试类，重写为 `TestSendResponseHeaders` 和 `TestStreamResponseBody`**

删除整个 `TestSendResponse` 类（测试的是已删除的 `_send_response` 方法），用以下两个类替代：

```python
class TestSendResponseHeaders:
    """Test _send_response_headers with httpx.Response."""

    def test_sse_response_includes_cache_control_and_accel_buffering(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """SSE responses must include Cache-Control: no-cache and X-Accel-Buffering: no."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream", "transfer-encoding": "chunked"}
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        all_headers = b"".join(queued)
        assert b"Cache-Control: no-cache" in all_headers
        assert b"X-Accel-Buffering: no" in all_headers

    def test_sse_response_preserves_transfer_encoding(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """SSE responses must NOT strip Transfer-Encoding: chunked."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "text/event-stream", "transfer-encoding": "chunked"}
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        all_headers = b"".join(queued)
        assert b"transfer-encoding: chunked" in all_headers.lower()

    def test_non_sse_response_has_no_sse_headers(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Non-SSE responses must NOT include SSE-specific headers."""
        mock_response = make_mock_httpx_response(
            headers={"content-type": "application/json", "content-length": "42"}
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        all_headers = b"".join(queued)
        assert b"Cache-Control: no-cache" not in all_headers
        assert b"X-Accel-Buffering: no" not in all_headers

    def test_uses_reason_phrase_not_reason(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Status line must use reason_phrase (httpx attr), not reason (requests attr)."""
        mock_response = make_mock_httpx_response(
            status_code=201, reason_phrase="Created"
        )
        queued: list[bytes] = []
        plugin.client = Mock(queue=lambda mv: queued.append(bytes(mv)))

        plugin._send_response_headers(mock_response)

        status_line = bytes(queued[0])
        assert b"201 Created" in status_line


class TestStreamResponseBody:
    """Test _stream_response_body with httpx.Response."""

    def test_forwards_all_chunks_immediately(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Each chunk from iter_bytes() is queued immediately — no buffering."""
        chunks = [b"data: token1\n\n", b"data: token2\n\n", b"data: [DONE]\n\n"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        queued_chunks: list[bytes] = []
        plugin.client = Mock(
            queue=lambda mv: queued_chunks.append(bytes(mv)),
            connection=Mock(),
        )

        plugin._stream_response_body(mock_response)

        assert queued_chunks == chunks

    def test_stops_when_client_disconnects(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Streaming stops gracefully when client connection is lost."""
        chunks = [b"chunk1", b"chunk2", b"chunk3"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        plugin.client = Mock(queue=Mock(), connection=None)  # disconnected

        bytes_sent, chunks_sent = plugin._stream_response_body(mock_response)

        # No chunks should be sent (client was disconnected from the start)
        assert chunks_sent == 0

    def test_handles_broken_pipe_gracefully(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """BrokenPipeError during streaming exits cleanly without raising."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        call_count = 0

        def queue_with_error(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise BrokenPipeError()

        plugin.client = Mock(queue=queue_with_error, connection=Mock())

        # Must not raise
        plugin._stream_response_body(mock_response)

    def test_handles_connection_reset_error_gracefully(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """ConnectionResetError during streaming exits cleanly without raising."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        call_count = 0

        def queue_with_reset(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise ConnectionResetError()

        plugin.client = Mock(queue=queue_with_reset, connection=Mock())

        plugin._stream_response_body(mock_response)  # must not raise

    def test_handles_os_error_errno_32_gracefully(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """OSError with errno=32 (broken pipe) exits cleanly without raising."""
        chunks = [b"chunk1", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        call_count = 0

        def queue_with_oserror(mv: memoryview) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                err = OSError("Broken pipe")
                err.errno = 32
                raise err

        plugin.client = Mock(queue=queue_with_oserror, connection=Mock())

        plugin._stream_response_body(mock_response)  # must not raise

    def test_skips_empty_chunks(self, plugin: FlowProxyWebServerPlugin) -> None:
        """Empty byte strings from iter_bytes() are skipped, not forwarded."""
        chunks = [b"chunk1", b"", b"chunk2"]
        mock_response = make_mock_httpx_response(chunks=chunks)

        queued_chunks: list[bytes] = []
        plugin.client = Mock(
            queue=lambda mv: queued_chunks.append(bytes(mv)),
            connection=Mock(),
        )

        plugin._stream_response_body(mock_response)

        assert queued_chunks == [b"chunk1", b"chunk2"]
```

- [ ] **Step 3: 重写 `TestHandleRequest` 测试类**

```python
class TestHandleRequest:
    """Test handle_request with httpx mock."""

    def test_handle_request_success_queues_response(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Successful request queues status line and body chunks."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b'{"model": "gpt-3.5-turbo"}'
        request.headers = {b"content-type": (b"application/json", b"")}

        with mock_httpx_stream(plugin, chunks=[b'{"response": "ok"}']):
            plugin.handle_request(request)

        # Status line (200 OK) must be queued
        queued_calls = plugin.client.queue.call_args_list
        all_bytes = b"".join(bytes(c[0][0]) for c in queued_calls)
        assert b"200" in all_bytes

    def test_handle_request_failure_sends_error(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Exceptions during request processing trigger _send_error()."""
        request = Mock(spec=HttpParser)
        request.method = b"GET"
        request.path = b"/v1/models"
        request.headers = {}

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
        with (
            patch("httpx.Client", side_effect=Exception("Connection failed")),
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
            patch.object(plugin, "_send_error") as mock_error,
        ):
            plugin.handle_request(request)
            mock_error.assert_called_once()

    def test_handle_request_timeout_configuration(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """httpx.Timeout must be configured with connect=30s and read=600s."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.body = b"{}"
        request.headers = {}

        captured_timeout: httpx.Timeout | None = None

        def capture_stream(**kwargs: Any) -> Mock:
            nonlocal captured_timeout
            captured_timeout = kwargs.get("timeout")
            mock_resp = make_mock_httpx_response()
            mock_resp.__enter__ = Mock(return_value=mock_resp)
            mock_resp.__exit__ = Mock(return_value=False)
            return mock_resp

        mock_client = Mock()
        mock_client.stream.side_effect = capture_stream
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")
        with (
            patch("httpx.Client", return_value=mock_client),
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
        ):
            plugin.handle_request(request)

        assert isinstance(captured_timeout, httpx.Timeout)
        assert captured_timeout.connect == 30.0
        assert captured_timeout.read == 600.0

    def test_handle_request_remote_protocol_error_does_not_send_error(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """httpx.RemoteProtocolError is logged but does NOT trigger _send_error()."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/messages"
        request.body = b"{}"
        request.headers = {}

        mock_token_result = ({"clientId": "test"}, "test-config", "test-token")

        mock_client = Mock()
        mock_client.stream.side_effect = httpx.RemoteProtocolError(
            "Server disconnected", request=Mock()
        )
        mock_client.__enter__ = Mock(return_value=mock_client)
        mock_client.__exit__ = Mock(return_value=False)

        with (
            patch("httpx.Client", return_value=mock_client),
            patch.object(plugin, "_get_config_and_token", return_value=mock_token_result),
            patch.object(plugin, "_send_error") as mock_error,
        ):
            plugin.handle_request(request)
            mock_error.assert_not_called()

    def test_handle_request_with_buffer_body(
        self, plugin: FlowProxyWebServerPlugin
    ) -> None:
        """Request body read from buffer attribute when body attribute is absent."""
        request = Mock(spec=HttpParser)
        request.method = b"POST"
        request.path = b"/v1/chat/completions"
        request.buffer = bytearray(b'{"model": "gpt-3.5-turbo"}')
        request.headers = {}
        delattr(request, "body")

        with mock_httpx_stream(plugin):
            plugin.handle_request(request)

        # Status line must be queued — confirms happy path was taken, not error path
        queued_calls = plugin.client.queue.call_args_list
        all_bytes = b"".join(bytes(c[0][0]) for c in queued_calls)
        assert b"200" in all_bytes
```

- [ ] **Step 4: 运行新测试，确认全部失败（TDD 红灯阶段）**

```bash
cd /Users/kang/Projects/flow-proxy && poetry run pytest tests/test_web_server_plugin.py -v 2>&1 | tail -30
```

预期：`TestSendResponseHeaders`、`TestStreamResponseBody`、`TestHandleRequest` 中的测试因代码尚未更新而 FAIL（`AttributeError: requests` 或 `AttributeError: _send_response_headers` 不接受 `httpx.Response`）。这是正确的起点，继续 Task 2 的实现步骤。

- [ ] **Step 5: 完成 Task 2 的全部实现步骤后，运行测试确认全部通过**

```bash
cd /Users/kang/Projects/flow-proxy && poetry run pytest tests/test_web_server_plugin.py -v
```

预期：所有测试 PASS。

- [ ] **Step 6: 运行完整测试套件确认无回归**

```bash
cd /Users/kang/Projects/flow-proxy && poetry run pytest -v
```

预期：所有测试 PASS。

- [ ] **Step 7: Commit**

```bash
cd /Users/kang/Projects/flow-proxy && \
git add flow_proxy_plugin/plugins/web_server_plugin.py \
        tests/test_web_server_plugin.py \
        pyproject.toml poetry.lock && \
git commit -m "feat: replace requests with httpx for zero-buffering SSE streaming"
```

---

## Chunk 3: 代码质量检查与收尾

### Task 4: 全量 lint 检查

**Files:**
- 无新文件，检查已修改文件

- [ ] **Step 1: 运行完整 lint**

```bash
cd /Users/kang/Projects/flow-proxy && make lint
```

预期：Ruff、MyPy、Pylint 均无报错。常见问题：
- MyPy 找不到 `httpx` 类型 → httpx 自带 py.typed，应自动识别
- Pylint `too-many-locals` → `handle_request` 有约 11 个局部变量，pylint 上限为 15，不应触发

- [ ] **Step 2: 如有 lint 问题，逐一修复后重跑**

```bash
cd /Users/kang/Projects/flow-proxy && make lint
```

预期：Clean。

- [ ] **Step 3: 运行覆盖率**

```bash
cd /Users/kang/Projects/flow-proxy && make test-cov
```

预期：`web_server_plugin.py` 覆盖率不低于改造前（原为 ~80% 左右）。

- [ ] **Step 4: Commit lint 修复（如有）**

```bash
cd /Users/kang/Projects/flow-proxy && \
git add -u && \
git commit -m "fix: lint issues after httpx migration"
```

如无修复则跳过此步。
