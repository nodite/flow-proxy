# Flow Proxy Plugin

一个基于 proxy.py 框架的插件，用于处理 Flow LLM Proxy 的身份验证和请求代理，支持 Round-robin 负载均衡。

## 项目概述

Flow Proxy Plugin 是一个强大的代理插件，为 Flow LLM Proxy 服务提供自动化的身份验证和请求转发功能。该插件通过 Round-robin 负载均衡策略在多个认证配置之间分配请求，确保高可用性和负载分散。

## 核心功能

- **自动身份验证**: 自动生成和管理 JWT 令牌，无需在请求中包含认证信息
- **Round-robin 负载均衡**: 在多个认证配置之间循环分配请求，实现负载分散
- **透明代理**: 无缝转发请求到 Flow LLM Proxy 服务，保持原始请求内容
- **自动故障转移**: 当某个配置失败时，自动切换到下一个可用配置
- **全面的错误处理**: 详细的错误日志和自动重试机制
- **配置管理**: 基于 JSON 的配置文件，支持多个认证配置和验证

## 快速开始

### 安装

```bash
# 克隆仓库
git clone <repository-url>
cd flow-proxy-plugin

# 安装 Poetry
curl -sSL https://install.python-poetry.org | python3 -

# 安装依赖
poetry install
```

### 配置

```bash
# 创建配置文件
cp secrets.json.template secrets.json

# 编辑配置文件，填入认证信息
# 至少需要配置 clientId, clientSecret, tenant

# 保护配置文件
chmod 600 secrets.json
```

### 运行

```bash
# 使用默认配置启动
poetry run flow-proxy

# 或使用自定义配置
poetry run flow-proxy --port 9000 --host 0.0.0.0 --log-level INFO
```

## Docker Compose 部署

```bash
# 1. 准备配置文件
cp secrets.json.template secrets.json
vim secrets.json  # 编辑填入认证信息

# 2. 启动服务
docker-compose up -d

# 3. 查看日志
docker-compose logs -f

# 4. 停止服务
docker-compose down
```

### 测试

插件支持两种使用模式：

#### 模式 1: 反向代理模式（推荐）

直接访问代理服务器，无需配置代理设置。代理会自动转发请求到 Flow LLM Proxy 并添加认证。

```bash
# 测试 GET 请求
curl http://localhost:8899/v1/models

# 测试 POST 请求
curl -X POST http://localhost:8899/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]}'

# 测试流式响应
curl -X POST http://localhost:8899/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'
```

#### 模式 2: 正向代理模式

使用 `-x` 参数配置代理，适合需要代理其他服务的场景。

```bash
# 使用代理访问（注意：URL 使用 http://，代理会转发到 HTTPS）
curl -x http://localhost:8899 http://flow.ciandt.com/flow-llm-proxy/v1/models

# 或者设置环境变量
export HTTP_PROXY=http://localhost:8899
curl http://flow.ciandt.com/flow-llm-proxy/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]}'
```

**注意**:
- **反向代理模式**：直接访问 `http://localhost:8899/v1/...`，无需指定完整 URL
- **正向代理模式**：需要使用 `-x` 参数，URL 使用 `http://`（不是 `https://`）
- 代理会自动添加 JWT 认证令牌到请求头
- 代理会自动将请求转发到 `https://flow.ciandt.com/flow-llm-proxy`


## 配置示例

### secrets.json 配置文件

```json
[
  {
    "name": "primary-1",
    "clientId": "your-client-id-1",
    "clientSecret": "your-client-secret-1",
    "tenant": "your-tenant-1"
  },
  {
    "name": "primary-2",
    "clientId": "your-client-id-2",
    "clientSecret": "your-client-secret-2",
    "tenant": "your-tenant-2"
  },
  {
    "name": "backup",
    "clientId": "your-client-id-3",
    "clientSecret": "your-client-secret-3",
    "tenant": "your-tenant-3"
  }
]
```

**配置字段说明**:
- **必需字段** (用于 JWT 令牌生成):
  - `clientId`: Flow 认证的客户端标识符
  - `clientSecret`: Flow 认证的客户端密钥
  - `tenant`: Flow 认证的租户标识符

- **可选字段** (用于日志记录和配置管理):
  - `name`: 配置的可读名称
  - `agent`: 代理标识符
  - `appToAccess`: 应用程序标识符

### 环境变量配置

```bash
# .env 文件
FLOW_PROXY_PORT=8899
FLOW_PROXY_HOST=127.0.0.1
FLOW_PROXY_LOG_LEVEL=INFO
FLOW_PROXY_SECRETS_FILE=secrets.json
FLOW_PROXY_LOG_FILE=flow_proxy_plugin.log
```

## Round-robin 负载均衡

插件使用 Round-robin（轮询）策略在多个认证配置之间分配请求：

### 工作原理

1. **顺序分配**: 请求按顺序使用不同的认证配置
2. **自动循环**: 到达最后一个配置后，自动从第一个开始
3. **故障处理**: 失败的配置自动跳过，使用下一个可用配置
4. **配置日志**: 每个请求都记录使用的配置名称

### 配置示例

**基本负载均衡** (2个配置，1:1 比例):

```json
[
  {"name": "config1", "clientId": "...", "clientSecret": "...", "tenant": "..."},
  {"name": "config2", "clientId": "...", "clientSecret": "...", "tenant": "..."}
]
```

请求分配: config1 → config2 → config1 → config2 → ...

**加权负载均衡** (3:1 比例):

```json
[
  {"name": "primary-1", "clientId": "...", "clientSecret": "...", "tenant": "..."},
  {"name": "primary-2", "clientId": "...", "clientSecret": "...", "tenant": "..."},
  {"name": "primary-3", "clientId": "...", "clientSecret": "...", "tenant": "..."},
  {"name": "backup", "clientId": "...", "clientSecret": "...", "tenant": "..."}
]
```

请求分配: primary-1 → primary-2 → primary-3 → backup → primary-1 → ...
(primary 配置接收 75% 的请求，backup 接收 25%)

## 架构概述

```
┌─────────────┐
│ Client App  │
└──────┬──────┘
       │ HTTP Request
       ▼
┌──────────────────────────────────┐
│    FlowProxyPlugin (proxy.py)    │
│  ┌────────────────────────────┐  │
│  │  1. SecretsManager         │  │
│  │     - Load secrets.json    │  │
│  │     - Validate configs     │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │  2. LoadBalancer           │  │
│  │     - Round-robin          │  │
│  │     - Failure tracking     │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │  3. JWTGenerator           │  │
│  │     - Generate JWT tokens  │  │
│  └────────────────────────────┘  │
│  ┌────────────────────────────┐  │
│  │  4. RequestForwarder       │  │
│  │     - Modify headers       │  │
│  │     - Forward requests     │  │
│  └────────────────────────────┘  │
└──────┬───────────────────────────┘
       │ Authenticated Request
       ▼
┌──────────────┐
│ Flow LLM     │
│ Proxy        │
└──────────────┘
```

### 核心组件

1. **FlowProxyPlugin**: 主插件类，协调所有组件
2. **SecretsManager**: 管理配置文件的加载和验证
3. **LoadBalancer**: 实现 Round-robin 负载均衡策略
4. **JWTGenerator**: 生成 Flow LLM Proxy 所需的 JWT 令牌
5. **RequestForwarder**: 处理请求修改和转发

## 开发环境

### 代码质量工具

本项目使用以下代码质量工具（通过 Poetry 配置）：

- **Ruff**: 快速的 Python linter 和格式化工具
- **MyPy**: 静态类型检查
- **Pylint**: 代码质量分析
- **Commitizen**: 规范化提交消息

### 安装 Pre-commit Hooks

```bash
poetry run pre-commit install
```

### 运行测试

```bash
# 运行所有测试
poetry run pytest

# 带覆盖率报告
poetry run pytest --cov=flow_proxy_plugin

# 生成 HTML 覆盖率报告
poetry run pytest --cov=flow_proxy_plugin --cov-report=html
```

### 代码质量检查

```bash
# Ruff linting
poetry run ruff check flow_proxy_plugin

# Ruff 格式化
poetry run ruff format flow_proxy_plugin

# MyPy 类型检查
poetry run mypy flow_proxy_plugin

# Pylint 代码分析
poetry run pylint flow_proxy_plugin
```

### 提交规范

本项目使用 Conventional Commits 规范。使用 commitizen 创建规范的提交消息：

```bash
poetry run cz commit
```

## 文档

- **[使用指南](docs/使用指南.md)** - 完整的用户使用指南，包括安装、配置、使用和故障排除
- **[开发指南](docs/开发指南.md)** - 开发者文档，包括 API 文档、架构说明和扩展指南
- **[部署运维](docs/部署运维.md)** - 生产环境部署和运维指南
- **[Flow LLM Proxy 官方文档](https://flow.ciandt.com/help/en/help/articles/8421153-overview-and-configuration)** - Flow LLM Proxy 概述与配置

## 许可证

[添加您的许可证信息]
