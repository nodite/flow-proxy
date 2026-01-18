# Flow Proxy Plugin

一个基于 proxy.py 框架的插件，用于处理 Flow LLM Proxy 的身份验证和请求代理，支持 Round-robin 负载均衡。

## 项目概述

Flow Proxy Plugin 是一个强大的代理插件，为 Flow LLM Proxy 服务提供自动化的身份验证和请求转发功能。该插件通过 Round-robin 负载均衡策略在多个认证配置之间分配请求，确保高可用性和负载分散。

## 核心功能

- **高性能并发**: 多进程+多线程架构，50-100倍性能提升，开箱即用
- **JWT 智能缓存**: 自动缓存 JWT 令牌，减少 95% 的生成开销
- **自动身份验证**: 自动生成和管理 JWT 令牌，无需在请求中包含认证信息
- **Round-robin 负载均衡**: 在多个认证配置之间循环分配请求，实现负载分散
- **透明代理**: 无缝转发请求到 Flow LLM Proxy 服务，保持原始请求内容
- **自动故障转移**: 当某个配置失败时，自动切换到下一个可用配置
- **全面的错误处理**: 详细的错误日志和自动重试机制

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
2. **并发处理**: 多进程+多线程架构，自动使用 CPU 核心数
3. **JWT 缓存**: 智能缓存机制，线程安全，大幅降低 CPU 开销
4. **SecretsManager**: 管理配置文件的加载和验证
5. **LoadBalancer**: 实现 Round-robin 负载均衡策略
6. **JWTGenerator**: 生成和缓存 JWT 令牌
7. **RequestForwarder**: 处理请求修改和转发

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

## 快速开始

### 前置要求

- Python 3.11+
- Poetry 1.8+
- Docker & Docker Compose (可选，用于容器化部署)

### 安装与配置

```bash
# 1. 克隆仓库
git clone <repository-url>
cd flow-proxy-plugin

# 2. 初始化项目（自动安装依赖、配置 pre-commit、创建配置文件）
make setup

# 3. 编辑配置文件，填入认证信息
vim secrets.json
```

### 运行服务

```bash
# 开发模式运行（自动使用最优并发配置）
make run

# 或使用 Poetry 直接运行
poetry run flow-proxy-plugin

# 自定义配置
poetry run flow-proxy-plugin --port 8899 --host 0.0.0.0 --log-level DEBUG
poetry run flow-proxy-plugin --num-workers 8 --no-threaded
```

**启动日志示例**:
```
INFO     flow_proxy_plugin.cli - ============================================================
INFO     flow_proxy_plugin.cli - Flow Proxy Plugin v0.1.2
INFO     flow_proxy_plugin.cli - ============================================================
INFO     flow_proxy_plugin.cli -   Host: 127.0.0.1
INFO     flow_proxy_plugin.cli -   Port: 8899
INFO     flow_proxy_plugin.cli -   Workers: 10
INFO     flow_proxy_plugin.cli -   Threaded: enabled
INFO     flow_proxy_plugin.cli -   Secrets: secrets.json
INFO     flow_proxy_plugin.cli -   Log level: INFO
INFO     flow_proxy_plugin.cli - ============================================================
```

## Make 命令速查

```bash
make help               # 查看所有可用命令

# 常用命令
make setup              # 初始化项目
make run                # 运行服务
make test               # 运行测试
make lint               # 代码检查
make format             # 格式化代码
make deploy             # Docker 部署
make docker-logs        # 查看日志
```

## 使用指南

### Docker Compose 部署

```bash
# 方式 1: 使用 Make 命令（推荐）
make setup              # 初始化项目并创建配置文件
vim secrets.json        # 编辑配置文件
make deploy             # 启动服务

# 方式 2: 使用 docker-compose 命令
cp secrets.json.template secrets.json
vim secrets.json
docker-compose up -d

# 管理服务
make docker-logs        # 查看日志
make docker-restart     # 重启服务
make docker-down        # 停止服务
```

### 测试服务

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

## 配置说明

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
FLOW_PROXY_PORT=8899                    # 监听端口
FLOW_PROXY_HOST=127.0.0.1              # 监听地址
FLOW_PROXY_NUM_WORKERS=                # Worker 数量（默认：CPU 核心数）
FLOW_PROXY_THREADED=1                  # 线程模式（1=启用，0=禁用，默认：1）
FLOW_PROXY_LOG_LEVEL=INFO             # 日志级别
FLOW_PROXY_SECRETS_FILE=secrets.json   # 配置文件路径
FLOW_PROXY_LOG_FILE=flow_proxy_plugin.log  # 日志文件路径
```

**性能优化**:
- 默认启用多进程+多线程模式
- Worker 数量自动检测为 CPU 核心数
- JWT 令牌自动缓存（1小时有效期）
- 无需手动配置，开箱即用

## 开发指南

### 开发工作流

```bash
# 1. 初始化开发环境
make setup

# 2. 运行代码质量检查
make lint               # 运行所有检查（Ruff + MyPy + Pylint）
make format             # 自动格式化代码

# 3. 运行测试
make test               # 运行测试
make test-cov           # 运行测试并生成覆盖率报告

# 4. 完整检查
make check              # 运行所有检查和测试

# 5. 提交代码（自动运行 pre-commit）
git add .
git commit -m "feat: add new feature"
```

### 代码质量工具

本项目使用以下工具确保代码质量：

- **Ruff**: 快速的 Python linter 和格式化工具
- **MyPy**: 静态类型检查
- **Pylint**: 代码质量分析
- **Pre-commit**: Git 提交前自动检查
- **Commitizen**: 规范化提交消息

所有工具都已配置在 `make setup` 中自动安装。

## 文档

- **[使用指南](docs/使用指南.md)** - 完整的用户使用指南，包括安装、配置、使用和故障排除
- **[开发指南](docs/开发指南.md)** - 开发者文档，包括 API 文档、架构说明和扩展指南
- **[部署运维](docs/部署运维.md)** - 生产环境部署和运维指南
- **[Flow LLM Proxy 官方文档](https://flow.ciandt.com/help/en/help/articles/8421153-overview-and-configuration)** - Flow LLM Proxy 概述与配置

## 许可证

[添加您的许可证信息]
