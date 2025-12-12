# 需求文档

## 介绍

本文档规定了 FlowProxyPlugin 的需求，该插件与 proxy.py 集成，接收包含原始认证信息（clientId、clientSecret、tenant）的请求，生成 Flow 认证 token，并将请求代理到 Flow LLM Proxy 服务。

## 术语表

- **FlowProxyPlugin**: 用于 proxy.py 的自定义插件，处理 Flow 认证 token 生成和请求代理
- **secrets.json**: 本地配置文件，包含多个认证配置的数组，每个配置包含 clientId、clientSecret 和 tenant
- **认证配置**: secrets.json 数组中的单个配置对象，包含完整的认证信息
- **Flow 认证 Token**: 基于选定认证配置生成的用于 Flow LLM Proxy 的 JWT 令牌
- **Flow LLM Proxy**: 目标 LLM 代理服务 (https://flow.ciandt.com/flow-llm-proxy)
- **proxy.py**: 将托管我们插件的 HTTP 代理框架
- **Round-robin**: 循环轮换的负载均衡策略，按顺序使用不同的认证配置
- **Load Balancer**: 负责在多个认证配置之间分配请求的组件
- **Token Generator**: 负责根据选定的认证配置生成 Flow 认证 token 的组件
- **Request Forwarder**: 将带有生成 token 的请求转发到 Flow LLM Proxy 的组件

## 需求

### 需求 1

**用户故事:** 作为开发者，我希望插件从本地 secrets.json 文件读取多个认证信息配置，以便支持负载均衡和高可用性。

#### 验收标准

1. WHEN 插件初始化时 THEN 系统应从 secrets.json 文件加载认证信息数组
2. WHEN secrets.json 文件不存在时 THEN 系统应记录错误并拒绝启动
3. WHEN secrets.json 文件格式无效时 THEN 系统应记录错误并拒绝启动
4. WHEN secrets.json 数组为空或缺少必需字段时 THEN 系统应记录错误并拒绝启动
5. WHEN 成功加载认证信息数组时 THEN 系统应记录加载的配置数量以供验证

### 需求 2

**用户故事:** 作为客户端应用程序，我希望发送标准的 HTTP 请求，插件自动处理认证而无需在请求中包含认证信息。

#### 验收标准

1. WHEN 接收到任何 HTTP 请求时 THEN 系统应使用本地加载的认证信息
2. WHEN 请求是针对 LLM API 的时 THEN 系统应自动生成认证 token
3. WHEN 请求格式正确时 THEN 系统应继续处理请求
4. WHEN 请求包含无效内容时 THEN 系统应以 400 Bad Request 拒绝请求
5. WHEN 系统无法处理请求时 THEN 系统应返回适当的错误状态码

### 需求 3

**用户故事:** 作为 token 生成系统，我希望使用 Round-robin 策略从多个认证配置中选择并生成有效的 Flow 认证 token，以便实现负载均衡。

#### 验收标准

1. WHEN 需要生成 token 时 THEN 系统应使用 Round-robin 策略选择下一个可用的认证配置
2. WHEN 生成 token 时 THEN 系统应使用 HS256 算法和正确的 JWT 结构
3. WHEN token 生成失败时 THEN 系统应尝试下一个认证配置或返回 500 Internal Server Error
4. WHEN token 生成成功时 THEN 系统应在转发的请求中包含该 token
5. WHEN 所有认证配置都失败时 THEN 系统应记录错误并返回适当的错误响应

### 需求 4

**用户故事:** 作为代理系统，我希望将带有生成 token 的请求转发到 Flow LLM Proxy，以便客户端可以无缝访问 LLM 服务。

#### 验收标准

1. WHEN token 生成成功时 THEN 系统应将请求转发到 https://flow.ciandt.com/flow-llm-proxy
2. WHEN 转发请求时 THEN 系统应保留所有原始头并移除原始认证信息
3. WHEN 转发请求时 THEN 系统应在 Authorization 头中包含生成的 Flow 认证 token
4. WHEN 目标服务响应时 THEN 系统应将响应原样返回给原始客户端
5. WHEN 目标服务不可达时 THEN 系统应返回 502 Bad Gateway

### 需求 5

**用户故事:** 作为系统管理员，我希望对身份验证事件进行全面记录，以便监控和排除代理故障。

#### 验收标准

1. WHEN 使用 Round-robin 选择认证配置时 THEN 系统应记录正在使用的认证配置的 name 字段
2. WHEN 身份验证成功时 THEN 系统应记录客户端标识符、使用的配置名称和时间戳
3. WHEN 身份验证失败时 THEN 系统应记录失败原因、配置名称和客户端 IP
4. WHEN 转发请求时 THEN 系统应记录目标端点、使用的配置名称和响应状态
5. WHEN 发生错误时 THEN 系统应记录详细的错误信息以供调试
6. WHEN 插件启动或停止时 THEN 系统应记录操作状态变化

### 需求 6

**用户故事:** 作为负载均衡系统，我希望使用 Round-robin 策略在多个认证配置之间分配请求，以便实现负载分散和高可用性。

#### 验收标准

1. WHEN 处理连续请求时 THEN 系统应按顺序轮换使用不同的认证配置并记录配置名称
2. WHEN 到达认证配置数组末尾时 THEN 系统应重新从第一个配置开始轮换
3. WHEN 某个认证配置失败时 THEN 系统应跳过该配置并使用下一个可用配置
4. WHEN 所有认证配置都不可用时 THEN 系统应记录错误并返回服务不可用状态
5. WHEN 系统重启时 THEN Round-robin 计数器应重置为初始状态

### 需求 7

**用户故事:** 作为开发者，我希望插件处理不同的 HTTP 方法和内容类型，以便支持所有 LLM API 操作。

#### 验收标准

1. WHEN 接收 GET 请求时 THEN 系统应处理模型列表操作
2. WHEN 接收 POST 请求时 THEN 系统应处理聊天完成操作
3. WHEN 接收带有 JSON 内容的请求时 THEN 系统应保留请求体
4. WHEN 接收带有流式响应的请求时 THEN 系统应支持流式传递
5. WHEN 接收 OPTIONS 请求时 THEN 系统应适当处理 CORS 预检