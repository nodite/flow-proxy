# 设计文档

## 概述

FlowProxyPlugin 是一个基于 proxy.py 框架的自定义插件，用于处理 Flow LLM Proxy 的身份验证和请求代理。该插件从本地 secrets.json 文件读取认证信息，生成 JWT 令牌，并将客户端请求透明地转发到 Flow LLM Proxy 服务。

## 架构

### 整体架构

```
客户端应用 → FlowProxyPlugin → Flow LLM Proxy
                ↓
           secrets.json
```

### 组件交互流程

1. **初始化阶段**: 插件启动时从 secrets.json 加载认证信息数组，初始化负载均衡器
2. **负载均衡阶段**: 使用 Round-robin 策略选择下一个可用的认证配置
3. **令牌生成阶段**: 基于选定的认证配置生成 JWT 令牌
4. **代理阶段**: 将带有认证令牌的请求转发到 Flow LLM Proxy
5. **响应阶段**: 将 Flow LLM Proxy 的响应返回给客户端
6. **错误处理阶段**: 如果认证失败，标记配置为失败并尝试下一个配置

## 组件和接口

### 1. FlowProxyPlugin 主类

```python
class FlowProxyPlugin(HttpProxyBasePlugin):
    """Flow LLM Proxy 认证插件主类"""

    def __init__(self, *args, **kwargs):
        """初始化插件，加载认证配置"""

    def before_upstream_connection(self, request: HttpParser) -> Optional[HttpParser]:
        """在建立上游连接前处理请求"""

    def handle_client_request(self, request: HttpParser) -> Optional[HttpParser]:
        """处理客户端请求，添加认证信息"""

    def handle_upstream_chunk(self, chunk: memoryview) -> memoryview:
        """处理上游响应数据"""
```

### 2. SecretsManager 配置管理器

```python
class SecretsManager:
    """管理 secrets.json 配置文件的加载和验证"""

    def load_secrets(self, file_path: str) -> List[Dict[str, str]]:
        """从文件加载认证信息数组"""

    def validate_secrets(self, secrets: List[Dict[str, str]]) -> bool:
        """验证认证信息数组的完整性"""

    def validate_single_config(self, config: Dict[str, str]) -> bool:
        """验证单个认证配置的完整性"""
```

### 3. LoadBalancer 负载均衡器

```python
class LoadBalancer:
    """实现 Round-robin 负载均衡策略"""

    def __init__(self, configs: List[Dict[str, str]], logger: Logger):
        """初始化负载均衡器，设置认证配置列表和日志记录器"""

    def get_next_config(self) -> Dict[str, str]:
        """使用 Round-robin 策略获取下一个认证配置并记录配置名称"""

    def mark_config_failed(self, config: Dict[str, str]) -> None:
        """标记配置失败，从可用列表中移除并记录"""

    def reset_failed_configs(self) -> None:
        """重置失败配置，重新加入可用列表"""

    def log_config_usage(self, config: Dict[str, str]) -> None:
        """记录正在使用的认证配置名称"""
```

### 4. JWTGenerator JWT 令牌生成器

```python
class JWTGenerator:
    """生成 Flow LLM Proxy 所需的 JWT 令牌"""

    def generate_token(self, config: Dict[str, str]) -> str:
        """根据认证配置生成 JWT 令牌"""

    def create_jwt_payload(self, config: Dict[str, str]) -> Dict:
        """创建 JWT 载荷"""

    def validate_token(self, token: str) -> bool:
        """验证生成的 JWT 令牌格式"""
```

### 5. RequestForwarder 请求转发器

```python
class RequestForwarder:
    """处理请求转发到 Flow LLM Proxy"""

    def modify_request_headers(self, request: HttpParser, jwt_token: str) -> HttpParser:
        """修改请求头，添加认证信息"""

    def get_target_url(self, original_path: str) -> str:
        """获取目标 Flow LLM Proxy URL"""

    def handle_forwarding_error(self, error: Exception) -> HttpResponse:
        """处理转发过程中的错误"""
```

## 数据模型

### secrets.json 配置文件结构

```json
[
    {
        "name": "config1",
        "agent": "simple_agent",
        "appToAccess": "llm-api",
        "clientId": "client-id-1",
        "clientSecret": "client-secret-1",
        "tenant": "tenant1"
    },
    {
        "name": "config2",
        "agent": "simple_agent",
        "appToAccess": "llm-api",
        "clientId": "client-id-2",
        "clientSecret": "client-secret-2",
        "tenant": "tenant2"
    }
]
```

### Round-robin 负载均衡状态

```python
class RoundRobinState:
    current_index: int = 0
    available_configs: List[Dict[str, str]]
    failed_configs: List[Dict[str, str]]
    total_requests: int = 0
```

### JWT 令牌结构

**Header:**
```json
{
    "alg": "HS256",
    "typ": "JWT"
}
```

**Payload:**
```json
{
    "clientId": "your-client-id",
    "clientSecret": "your-client-secret",
    "tenant": "your-tenant"
}
```

### HTTP 请求流程

**原始客户端请求:**
```
GET /v1/models HTTP/1.1
Host: localhost:8899
Content-Type: application/json
```

**转发到 Flow LLM Proxy 的请求:**
```
GET /v1/models HTTP/1.1
Host: flow.ciandt.com
Authorization: Bearer <generated-jwt-token>
Content-Type: application/json
```

## 正确性属性

*属性是一个特征或行为，应该在系统的所有有效执行中保持为真——本质上，是关于系统应该做什么的正式声明。属性作为人类可读规范和机器可验证正确性保证之间的桥梁。*

### 属性反思

在编写正确性属性之前，我需要识别和消除冗余：

- 属性 1（配置加载）和属性 5（日志记录）可以合并为一个综合的初始化属性
- 属性 2（请求处理）和属性 3（token 生成）可以合并为一个端到端的认证属性
- 属性 4（请求转发）和属性 6（HTTP 方法支持）可以合并为一个综合的代理属性
- 日志相关的多个属性可以合并为一个综合的日志属性

### 核心正确性属性

**属性 1: 配置数组加载和初始化**
*对于任何* 有效的 secrets.json 数组文件，插件初始化应该成功加载所有认证配置并初始化负载均衡器
**验证需求: 需求 1.1, 1.5**

**属性 2: Round-robin 负载均衡循环性**
*对于任何* 连续的请求序列，Round-robin 策略应该按顺序循环使用所有可用的认证配置
**验证需求: 需求 6.1, 6.2**

**属性 3: JWT 令牌生成往返**
*对于任何* 有效的认证配置，生成 JWT 令牌然后解析应该产生相同的 clientId、clientSecret 和 tenant 值
**验证需求: 需求 3.1, 3.2**

**属性 4: 配置失败转移**
*对于任何* 认证配置失败的情况，系统应该自动切换到下一个可用配置并继续处理请求
**验证需求: 需求 3.3, 6.3**

**属性 5: 请求代理保持性**
*对于任何* 有效的 HTTP 请求，代理后的请求应该保留原始请求的所有内容（除认证信息外），并添加正确的 Flow 认证 token
**验证需求: 需求 4.1, 4.2, 4.3**

**属性 6: 响应透明传递**
*对于任何* Flow LLM Proxy 的响应，系统应该将响应原样返回给客户端，不修改内容或状态码
**验证需求: 需求 4.4**

**属性 7: HTTP 方法支持完整性**
*对于任何* 支持的 HTTP 方法（GET、POST、OPTIONS），系统应该正确处理请求并保留请求体内容
**验证需求: 需求 7.1, 7.2, 7.3, 7.5**

**属性 8: 错误处理一致性**
*对于任何* 错误情况（配置错误、网络错误、token 生成失败），系统应该返回适当的 HTTP 状态码并记录详细错误信息
**验证需求: 需求 1.2, 1.3, 1.4, 3.5, 4.5**

**属性 9: 日志记录完整性**
*对于任何* 系统操作（初始化、请求处理、错误处理、负载均衡），系统应该记录相应的日志信息包含时间戳、配置名称和相关上下文
**验证需求: 需求 5.1, 5.2, 5.3, 5.4, 5.5, 5.6**

## 错误处理

### 配置错误处理
- **文件不存在**: 返回启动错误，记录详细日志
- **JSON 格式错误**: 返回配置错误，记录解析失败信息
- **缺少必需字段**: 返回验证错误，记录缺失字段列表

### 运行时错误处理
- **JWT 生成失败**: 返回 500 Internal Server Error
- **网络连接失败**: 返回 502 Bad Gateway
- **无效请求格式**: 返回 400 Bad Request

### 错误响应格式
```json
{
    "error": "error_code",
    "message": "详细错误描述",
    "timestamp": "2024-01-01T00:00:00Z"
}
```

## 测试策略

### 单元测试方法

单元测试将验证具体的组件行为和边界情况：

- **SecretsManager**: 测试配置文件加载、验证和错误处理
- **JWTGenerator**: 测试 JWT 生成、格式验证和签名
- **RequestForwarder**: 测试请求头修改和 URL 构建
- **错误处理**: 测试各种错误情况的响应

### 属性测试方法

属性测试将使用 **Hypothesis** 库验证系统在各种输入下的通用属性：

- 每个属性测试运行最少 100 次迭代以确保充分覆盖
- 使用智能生成器创建有效和无效的输入数据
- 每个属性测试必须用注释明确引用设计文档中的对应属性

**属性测试标记格式**: `**Feature: flow-proxy-plugin, Property {number}: {property_text}**`

### 测试数据生成策略

- **配置数据**: 生成有效和无效的 secrets.json 内容
- **HTTP 请求**: 生成各种方法、头部和请求体的组合
- **JWT 数据**: 生成有效和无效的认证信息组合
- **网络条件**: 模拟各种网络状态和响应
