# SSE 实时流式传输改造设计文档

**日期**：2026-03-11
**状态**：已批准
**影响范围**：Reverse Proxy 模式（FlowProxyWebServerPlugin）

---

## 背景与问题

当前 Reverse Proxy 模式使用 `requests` 库转发请求，存在以下缺陷导致 SSE 响应不实时且频繁中断：

1. **延迟主因**：`iter_content(chunk_size=8192)` 在客户端积累 8192 字节才触发迭代，LLM token（通常几十字节）会被缓冲。
2. **中断主因**：`_send_response_headers` 过滤掉了 `Transfer-Encoding: chunked`，客户端无法识别流式响应，超时后断开。
3. **次要问题**：`flush()` 是条件调用，proxy.py client 对象没有该方法，数据积压在队列中。

---

## 目标

- SSE 响应零缓冲，上游每个事件立即转发给客户端
- 消除因响应头缺失导致的客户端中断
- 保持对普通（非 SSE）响应的兼容性

---

## 方案：用 httpx 替换 requests

选择 `httpx` 而非修补 `requests` 的原因：
- `httpx.stream()` 是上下文管理器，无 `urllib3` 缓冲层，流式更可靠
- `response.iter_lines()` 原生支持按行迭代，适合 SSE 协议（换行分隔的文本事件）
- API 简洁，替换成本低

---

## 详细设计

### 1. 依赖变更（`pyproject.toml`）

- 添加：`httpx = "^0.28"`
- 移除：`requests`（及 dev 依赖中的 `types-requests`）

### 2. `_forward_request`（`web_server_plugin.py`）

**现在**：
```python
return requests.request(
    method=method, url=target_url, headers=headers,
    data=body, stream=True, timeout=(30, 600),
)
```

**改后**：使用 `httpx.Client` 并以上下文管理器方式进入流式模式。
在 `handle_request` 层用 `with client.stream(...)` 包裹整个转发+响应流程，确保连接在流结束前保持打开，结束后自动释放。

超时配置：`httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0)`

### 3. `_send_response_headers`（`web_server_plugin.py`）

**修改点**：
- 移除对 `transfer-encoding` 的过滤（现在直接跳过），改为：SSE 响应保留 `Transfer-Encoding: chunked`
- 对 SSE 响应（`Content-Type: text/event-stream`）补加：
  - `Cache-Control: no-cache`
  - `X-Accel-Buffering: no`（防止 nginx 等中间代理缓冲）

**SSE 检测**：`"text/event-stream" in response.headers.get("content-type", "")`

### 4. `_stream_response_body`（`web_server_plugin.py`）

**统一使用 `iter_bytes()` 处理所有响应**，不区分 SSE 与普通路径：

```python
for chunk in response.iter_bytes():
    self.client.queue(memoryview(chunk))
```

延迟问题的真正根源是 `requests` 的 `chunk_size=8192` 客户端缓冲，`iter_bytes()` 不指定 `chunk_size` 则上游发什么立即迭代，保留原始字节流（包括 SSE 的 `\n\n` 事件边界），无需按行重构。

> **为何不用 `iter_lines()`**：`iter_lines()` 会剥离行尾换行符，重构时无法可靠还原 SSE 事件边界（`\n\n`）。使用 `iter_bytes()` 直接透传字节流，避免所有重构风险。

移除 `flush()` 条件调用（proxy.py client 无此方法，调用无效）。

### 5. `handle_request` 结构调整

将 `_forward_request` 和 `_send_response` 合并到同一个 `with client.stream(...)` 上下文中，保证 httpx 连接在整个流式传输期间保持活跃。原有的后置日志（状态码 + config_name）需保留在上下文管理器内：

```python
def handle_request(self, request: HttpParser) -> None:
    ...
    with httpx.Client() as client:
        with client.stream(...) as response:
            self._send_response_headers(response)
            self._stream_response_body(response)
            log_func = self.logger.info if response.status_code < 400 else self.logger.warning
            log_func("← %d %s [%s]", response.status_code, response.reason_phrase, config_name)
```

**注意**：httpx 的状态原因短语属性为 `response.reason_phrase`（`str`），不是 `requests` 的 `response.reason`，所有引用需同步更新。

**`httpx.Client` 生命周期**：每次 `handle_request` 调用时创建新的 `httpx.Client()`，不在插件初始化时共享。原因：项目采用多进程架构，`fork()` 后共享连接池会导致文件描述符状态异常（与日志 handler 同样的问题）。每次创建有少量开销，但连接池在请求期间仍会复用底层 TCP 连接。

### 6. 异常类型映射

`_send_response` 中的 `requests` 异常需替换为 httpx 等价类型：

| requests | httpx |
|---|---|
| `requests.exceptions.ChunkedEncodingError` | `httpx.RemoteProtocolError` |
| `requests.exceptions.ConnectionError` | `httpx.ConnectError` |
| `requests.exceptions.Timeout` | `httpx.TimeoutException` |

### 7. 类型注解更新

所有私有方法的 `requests.Response` 类型注解替换为 `httpx.Response`。

---

## 不受影响的范围

- **Forward Proxy 模式**（`FlowProxyPlugin`）：proxy.py 框架原生处理流式，不涉及本次改动
- **JWT 认证、负载均衡、请求过滤**：逻辑不变，只替换 HTTP 客户端层
- **错误处理、日志、配置**：保持现有实现

---

## 测试策略

- 更新 `tests/test_web_server_plugin.py`：将 `unittest.mock.patch("requests.request")` 替换为 mock `httpx.Client.stream`
- 验证场景：
  - SSE 响应：每行立即转发，响应头包含 `Transfer-Encoding: chunked` 和 `Cache-Control: no-cache`
  - 普通 JSON 响应：正常转发，不受影响
  - 客户端中途断开：优雅退出，不报错
  - 上游连接超时：正确触发 `httpx.TimeoutException`
