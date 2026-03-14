# Flow Proxy Plugin

一个基于 proxy.py 框架的插件，用于处理 Flow LLM Proxy 的身份验证和请求代理，支持 Round-robin 负载均衡。

## 项目概述

Flow Proxy Plugin 是一个高性能代理插件，为 Flow LLM Proxy 服务提供自动化的 JWT 身份验证和请求转发功能。通过 Round-robin 负载均衡策略在多个认证配置之间分配请求，确保高可用性和负载分散。

## 核心功能

- **高性能并发**: 多进程+多线程架构，支持高并发请求
- **JWT 智能缓存**: 自动缓存 JWT 令牌（1小时有效期），减少约 95% 的认证开销
- **Round-robin 负载均衡**: 在多个认证配置之间循环分配请求，自动故障转移
- **两种代理模式**: 反向代理（直接访问）和正向代理（`-x` 参数）
- **SSE 中途错误通知**: 流式响应中断时，自动向客户端注入 `event: error` 事件，避免客户端无限等待
- **请求过滤**: 可配置规则，自动剥离特定请求字段和头部（如 `context_management`、`anthropic-beta`）
- **日志自动清理**: 自动清理过期日志文件，管理磁盘空间

## 架构概述

服务由两个并行运行的 proxy.py 插件组成，共享同一个 `ProcessServices` 单例（含 `LoadBalancer`、`JWTGenerator`、`httpx.Client` 等）。

```
                    ┌─────────────────────────────────────────────┐
                    │             proxy.py 进程池                  │
                    │                                             │
  curl -x ──────►  │  FlowProxyPlugin          ProcessServices   │
  (正向代理)        │  before_upstream_         ┌───────────────┐ │
                    │  connection()      ──────►│ LoadBalancer  │ │
                    │                           │ JWTGenerator  │ │
  curl http:// ──►  │  FlowProxyWebServer       │ httpx.Client  │ │
  (反向代理)        │  Plugin                   │ RequestFilter │ │
                    │  handle_request()  ──────►└───────────────┘ │
                    │       │                                     │
                    │  worker thread                              │
                    │  (streaming)                                │
                    │       │  chunk_queue + os.pipe()            │
                    │       ▼                                     │
                    │  read_from_descriptors()                    │
                    │  (main thread → client.queue())             │
                    └──────────────────┬──────────────────────────┘
                                       │ HTTPS + Bearer JWT
                                       ▼
                              flow.ciandt.com/flow-llm-proxy
```

**反向代理流式响应路径**（SSE / 大响应）：

1. `handle_request()` 获取 JWT、构建请求参数，立即启动 `_streaming_worker` 守护线程后返回
2. Worker 线程通过 `httpx` 流式读取响应，将 `_ResponseHeaders` → `bytes` chunks → `None`（哨兵）依次写入 `chunk_queue`，每次写入后向 `os.pipe` 写一字节通知
3. proxy.py 的 selector 感知到 `pipe_r` 可读，触发 `read_from_descriptors()`，主线程消费队列并调用 `client.queue()` 发送给客户端
4. 收到哨兵后，`_finish_stream()` 处理结束逻辑：后端出错且已发送 SSE 头时，注入 `event: error` 事件通知客户端

## 快速开始

### 前置要求

- Python 3.12+
- Poetry 1.8+
- Docker & Docker Compose（可选，用于容器化部署）

### 安装与配置

```bash
# 1. 克隆仓库
git clone <repository-url>
cd flow-proxy

# 2. 初始化项目（自动安装依赖、配置 pre-commit、创建配置文件）
make setup

# 3. 编辑配置文件，填入认证信息
vim secrets.json
```

### 运行服务

```bash
# 开发模式运行（DEBUG 日志）
make run

# 或使用 Poetry 直接运行
poetry run flow-proxy

# 自定义配置
poetry run flow-proxy --port 8899 --host 0.0.0.0 --log-level DEBUG
poetry run flow-proxy --num-workers 8 --no-threaded
```

**启动日志示例**:
```
INFO  Flow Proxy Plugin v1.0.0
INFO  ============================================================
INFO    Host: 127.0.0.1
INFO    Port: 8899
INFO    Workers: 10
INFO    Threaded: enabled
INFO    Client timeout: 600s
INFO    Secrets: secrets.json
INFO    Log level: INFO
INFO  ============================================================
```

## Make 命令速查

```bash
make help               # 查看所有可用命令

# 常用命令
make setup              # 初始化项目
make run                # 运行服务（DEBUG 日志）
make test               # 运行测试
make test-cov           # 运行测试并生成覆盖率报告
make lint               # 代码检查（Ruff + MyPy + Pylint）
make format             # 格式化代码
make check              # 运行所有检查和测试

# Docker 操作
make deploy             # Docker Compose 部署
make docker-logs        # 查看容器日志（持续跟踪）
make docker-restart     # 重启服务
make docker-down        # 停止服务
```

## 使用指南

### 模式 1: 反向代理模式（推荐）

直接访问代理服务器，无需配置代理设置。代理自动转发请求到 Flow LLM Proxy 并添加认证。

```bash
# 测试 GET 请求
curl http://localhost:8899/v1/models

# 测试 POST 请求
curl -X POST http://localhost:8899/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]}'

# 测试流式响应（SSE）
curl -X POST http://localhost:8899/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'
```

### 模式 2: 正向代理模式

使用 `-x` 参数配置代理，适合需要透明代理现有 HTTP 客户端的场景。

```bash
# 使用 -x 参数（URL 使用 http://，代理会自动转换为 HTTPS）
curl -x http://localhost:8899 http://flow.ciandt.com/flow-llm-proxy/v1/models

# 或设置环境变量
export HTTP_PROXY=http://localhost:8899
curl http://flow.ciandt.com/flow-llm-proxy/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": "Hello"}]}'
```

> **注意**: 正向代理模式中 URL 使用 `http://`（非 `https://`），代理会自动将请求转发到 `https://flow.ciandt.com/flow-llm-proxy`。

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
  }
]
```

**字段说明**:
- **必需**: `clientId`、`clientSecret`、`tenant`（用于 JWT 令牌生成）
- **可选**: `name`（可读名称，用于日志）、`agent`、`appToAccess`

### Round-robin 负载均衡

请求按顺序轮询各配置，失败配置自动跳过。通过重复条目实现加权分配：

```json
[
  {"name": "primary-1", ...},  // 每4个请求分配1个
  {"name": "primary-2", ...},  // 每4个请求分配1个
  {"name": "primary-3", ...},  // 每4个请求分配1个
  {"name": "backup",    ...}   // 每4个请求分配1个
]
```

### 环境变量

所有变量使用 `FLOW_PROXY_` 前缀，均有对应的 CLI 参数：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `FLOW_PROXY_PORT` | `8899` | 监听端口 |
| `FLOW_PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `FLOW_PROXY_NUM_WORKERS` | CPU 核心数 | Worker 进程数 |
| `FLOW_PROXY_THREADED` | `1` | 线程模式（`1`=启用，`0`=禁用） |
| `FLOW_PROXY_CLIENT_TIMEOUT` | `600` | 客户端连接超时（秒，范围 1–86400）；流式响应需 ≥ 后端首字节时间 |
| `FLOW_PROXY_LOG_LEVEL` | `INFO` | 日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`） |
| `FLOW_PROXY_SECRETS_FILE` | `secrets.json` | secrets.json 文件路径 |
| `FLOW_PROXY_LOG_DIR` | `logs` | 日志目录 |
| `FLOW_PROXY_LOG_CLEANUP_ENABLED` | `true` | 启用自动日志清理 |
| `FLOW_PROXY_LOG_RETENTION_DAYS` | `7` | 日志保留天数 |
| `FLOW_PROXY_LOG_CLEANUP_INTERVAL_HOURS` | `24` | 清理检查间隔（小时） |
| `FLOW_PROXY_LOG_MAX_SIZE_MB` | `100` | 日志目录最大大小（MB），超出则强制清理 |
| `FLOW_PROXY_PLUGIN_POOL_SIZE` | `64` | 每种插件类型的最大池化实例数 |

## Docker 部署

```bash
# 方式 1: 使用 Make（推荐）
make setup         # 初始化并创建配置文件
vim secrets.json   # 填入认证信息
make deploy        # 启动服务

# 方式 2: 直接使用 docker-compose
cp secrets.json.template secrets.json
vim secrets.json
docker-compose up -d
```

**容器资源限制**（默认配置）：
- 内存限制：512MB
- CPU 限制：2.0 核
- Worker 数量：2（减少内存占用）
- 健康检查：每 60 秒检测 `http://localhost:8899/`

## 开发

```bash
# 初始化开发环境
make setup

# 代码质量检查
make lint           # Ruff + MyPy + Pylint
make format         # 自动格式化

# 测试
make test           # 运行所有测试
make test-cov       # 带覆盖率报告
pytest -k jwt       # 运行特定测试
pytest tests/test_load_balancer.py::test_round_robin  # 运行单个测试

# 完整检查（提交前推荐）
make check
```

## 文档

- **[日志自动清理](docs/log-cleanup.md)** — 自动清理过期日志的功能说明
- **[日志过滤](docs/log-filtering.md)** — 日志输出过滤与级别控制
- **[Flow LLM Proxy 官方文档](https://flow.ciandt.com/help/en/help/articles/8421153-overview-and-configuration)** — Flow LLM Proxy 概述与配置

## 许可证

[添加您的许可证信息]
