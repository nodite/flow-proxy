# 日志过滤功能

## 概述

flow-proxy 实现了智能日志过滤功能，用于抑制预期的、非关键的警告信息，使日志输出更加清晰和易读。

## 过滤的日志类型

### 1. BrokenPipeError 警告

**来源**: `proxy.http.handler`

**原因**: 当客户端在流式响应过程中断开连接时，会触发 `BrokenPipeError`。这在以下场景中是完全正常的：

- 客户端超时并关闭连接
- 用户手动取消请求（如 Ctrl+C）
- 客户端已接收到所需的所有数据后主动断开
- 网络中断导致连接丢失

**处理方式**: 我们的代码已经在应用层优雅地处理了这些错误，因此底层的 proxy.py 警告是多余的，会造成日志污染。

### 2. ConnectionResetError 警告

**来源**: `proxy.http.handler`

**原因**: 类似于 BrokenPipeError，当连接被对端重置时触发。

**处理方式**: 同样在应用层已经妥善处理。

### 3. 冗余的访问日志（可选）

**来源**: `proxy.http.server.web`

**原因**: proxy.py 会自动记录所有请求的访问日志，格式如：
```
127.0.0.1:56798 - GET / - curl/8.14.1 - 11775.52ms
```

**处理方式**: 由于 flow-proxy 已经实现了自己的请求日志系统，提供了更丰富的信息（如配置名称、负载均衡状态等），可以选择性地过滤 proxy.py 的访问日志以减少重复信息。

## 实现细节

### 过滤器类

#### BrokenPipeFilter

```python
from flow_proxy_plugin.utils.log_filter import BrokenPipeFilter

# 过滤 BrokenPipeError 和 ConnectionResetError 警告
filter = BrokenPipeFilter()
logger = logging.getLogger("proxy.http.handler")
logger.addFilter(filter)
```

#### ProxyNoiseFilter

```python
from flow_proxy_plugin.utils.log_filter import ProxyNoiseFilter

# 过滤 proxy.py 的 INFO 级别访问日志
filter = ProxyNoiseFilter()
logger = logging.getLogger("proxy.http.server.web")
logger.addFilter(filter)
```

### 自动设置

在插件初始化时，过滤器会自动应用：

```python
from flow_proxy_plugin.utils.log_filter import setup_proxy_log_filters

# 在插件初始化时调用
setup_proxy_log_filters(
    suppress_broken_pipe=True,  # 抑制 BrokenPipeError 警告
    suppress_proxy_noise=True   # 抑制冗余的访问日志
)
```

## 日志级别控制

即使启用了过滤器，以下日志仍会正常显示：

- ✅ **ERROR 级别**: 所有错误都会被记录
- ✅ **其他 WARNING**: 非 BrokenPipeError 的警告会正常显示
- ✅ **应用层日志**: flow-proxy 自己的所有日志不受影响
- ✅ **DEBUG 模式**: 在 DEBUG 模式下，我们的代码会记录详细的连接断开信息

## 日志示例

### 过滤前

```
INFO     flow_proxy_plugin.plugins.web_server_plugin - → POST /v1/chat/completions
INFO     flow_proxy_plugin.plugins.web_server_plugin - Using config 'langgraph' (request #18)
WARNING  proxy.http.handler - BrokenPipeError when flushing buffer for client
INFO     proxy.http.server.web - 127.0.0.1:56798 - POST /v1/chat/completions - curl/8.14.1 - 11775.52ms
INFO     flow_proxy_plugin.plugins.web_server_plugin - ← 200 OK [langgraph]
```

### 过滤后

```
INFO     flow_proxy_plugin.plugins.web_server_plugin - → POST /v1/chat/completions
INFO     flow_proxy_plugin.plugins.web_server_plugin - Using config 'langgraph' (request #18)
INFO     flow_proxy_plugin.plugins.web_server_plugin - ← 200 OK [langgraph]
```

## 技术实现

### 过滤逻辑

过滤器使用 Python 的 `logging.Filter` 接口：

1. **检查日志源**: 确保只过滤特定 logger 的日志
2. **检查日志级别**: 只过滤特定级别的日志
3. **检查消息内容**: 通过消息内容判断是否为目标日志

### 测试覆盖

包含完整的单元测试和集成测试：

```bash
poetry run pytest tests/test_log_filter.py -v
```

测试覆盖：
- ✅ 正确过滤目标日志
- ✅ 不过滤其他日志
- ✅ 尊重日志级别
- ✅ 集成测试验证实际行为

## 配置选项

目前过滤器在插件初始化时自动启用，未来可以考虑添加配置选项：

```python
# 未来可能的配置方式
{
  "logging": {
    "suppress_broken_pipe_warnings": true,
    "suppress_proxy_access_logs": true
  }
}
```

## 注意事项

1. **不影响错误处理**: 过滤器只影响日志输出，不影响实际的错误处理逻辑
2. **调试友好**: 在 DEBUG 日志级别下，我们的代码仍会记录详细的连接信息
3. **选择性过滤**: 只过滤已知的、预期的、已妥善处理的警告
4. **保持透明**: ERROR 级别的日志永远不会被过滤

## 相关文件

- `flow_proxy_plugin/utils/log_filter.py` - 过滤器实现
- `flow_proxy_plugin/plugins/web_server_plugin.py` - Web 服务器插件（应用过滤器）
- `flow_proxy_plugin/plugins/proxy_plugin.py` - 代理插件（应用过滤器）
- `tests/test_log_filter.py` - 单元测试
