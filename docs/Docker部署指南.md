# Docker 部署指南

本指南介绍如何使用 Docker 和 Docker Compose 部署 Flow Proxy Plugin。

## 前置要求

- Docker 20.10+
- Docker Compose 2.0+（可选，用于简化部署）

## 快速开始

### 方式 1: 使用 Docker Compose（推荐）

1. **准备配置文件**

```bash
# 复制配置模板
cp secrets.json.template secrets.json

# 编辑配置文件，填入认证信息
vim secrets.json

# 创建日志目录
mkdir -p logs
```

2. **启动服务**

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

3. **测试服务**

```bash
# 测试连接
curl http://localhost:8899/v1/models
```

### 方式 2: 使用 Docker 命令

1. **构建镜像**

```bash
docker build -t flow-proxy-plugin:latest .
```

2. **运行容器**

```bash
# 基本运行
docker run -d \
  --name flow-proxy \
  -p 8899:8899 \
  -v $(pwd)/secrets.json:/app/secrets.json:ro \
  flow-proxy-plugin:latest

# 带日志挂载
docker run -d \
  --name flow-proxy \
  -p 8899:8899 \
  -v $(pwd)/secrets.json:/app/secrets.json:ro \
  -v $(pwd)/logs:/app/logs \
  flow-proxy-plugin:latest
```

3. **查看日志**

```bash
docker logs -f flow-proxy
```

4. **停止和删除容器**

```bash
docker stop flow-proxy
docker rm flow-proxy
```

## 配置说明

### 环境变量

可以通过环境变量覆盖默认配置：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `FLOW_PROXY_PORT` | 8899 | 代理服务监听端口 |
| `FLOW_PROXY_HOST` | 0.0.0.0 | 代理服务监听地址 |
| `FLOW_PROXY_LOG_LEVEL` | INFO | 日志级别 (DEBUG/INFO/WARNING/ERROR) |
| `FLOW_PROXY_SECRETS_FILE` | /app/secrets.json | 配置文件路径 |
| `FLOW_PROXY_LOG_FILE` | /app/logs/flow_proxy_plugin.log | 日志文件路径 |

### 使用环境变量示例

**Docker Compose:**

```yaml
environment:
  - FLOW_PROXY_PORT=9000
  - FLOW_PROXY_LOG_LEVEL=DEBUG
```

**Docker 命令:**

```bash
docker run -d \
  --name flow-proxy \
  -p 9000:9000 \
  -e FLOW_PROXY_PORT=9000 \
  -e FLOW_PROXY_LOG_LEVEL=DEBUG \
  -v $(pwd)/secrets.json:/app/secrets.json:ro \
  flow-proxy-plugin:latest
```

## 生产环境部署

### 1. 使用自定义端口

```yaml
services:
  flow-proxy:
    ports:
      - "9000:8899"  # 主机端口:容器端口
```

### 2. 资源限制

```yaml
services:
  flow-proxy:
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.5'
          memory: 256M
```

### 3. 日志管理

```yaml
services:
  flow-proxy:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### 4. 完整的生产配置示例

```yaml
version: '3.8'

services:
  flow-proxy:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: flow-proxy-plugin
    ports:
      - "8899:8899"
    volumes:
      - ./secrets.json:/app/secrets.json:ro
      - ./logs:/app/logs
    environment:
      - FLOW_PROXY_PORT=8899
      - FLOW_PROXY_HOST=0.0.0.0
      - FLOW_PROXY_LOG_LEVEL=INFO
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8899/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 5s
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.5'
          memory: 256M
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - flow-proxy-network

networks:
  flow-proxy-network:
    driver: bridge
```

## 常用命令

### 查看容器状态

```bash
docker-compose ps
```

### 查看实时日志

```bash
docker-compose logs -f flow-proxy
```

### 重启服务

```bash
docker-compose restart
```

### 更新服务

```bash
# 重新构建并启动
docker-compose up -d --build

# 或者分步执行
docker-compose build
docker-compose up -d
```

### 进入容器调试

```bash
docker-compose exec flow-proxy /bin/bash
```

### 清理资源

```bash
# 停止并删除容器
docker-compose down

# 同时删除卷
docker-compose down -v

# 删除镜像
docker rmi flow-proxy-plugin:latest
```

## 故障排查

### 1. 容器无法启动

```bash
# 查看容器日志
docker-compose logs flow-proxy

# 检查配置文件
docker-compose config
```

### 2. 配置文件未找到

确保 `secrets.json` 文件存在且路径正确：

```bash
# 检查文件是否存在
ls -la secrets.json

# 检查文件权限
chmod 644 secrets.json
```

### 3. 端口冲突

如果端口 8899 已被占用：

```bash
# 修改 docker-compose.yml 中的端口映射
ports:
  - "9000:8899"  # 使用其他主机端口
```

### 4. 健康检查失败

```bash
# 查看健康检查状态
docker inspect flow-proxy | grep -A 10 Health

# 手动测试健康检查
docker exec flow-proxy curl -f http://localhost:8899/
```

## 安全建议

1. **保护配置文件**
   ```bash
   chmod 600 secrets.json
   ```

2. **不要将 secrets.json 提交到版本控制**
   ```bash
   # 确保 .gitignore 包含
   echo "secrets.json" >> .gitignore
   ```

3. **使用 Docker secrets（Swarm 模式）**
   ```yaml
   secrets:
     flow_secrets:
       file: ./secrets.json

   services:
     flow-proxy:
       secrets:
         - flow_secrets
   ```

4. **定期更新基础镜像**
   ```bash
   docker-compose build --pull
   ```

## 监控和日志

### 查看容器资源使用

```bash
docker stats flow-proxy
```

### 导出日志

```bash
docker-compose logs --no-color > flow-proxy.log
```

### 集成日志系统

可以将日志发送到 ELK、Splunk 等日志系统：

```yaml
logging:
  driver: "syslog"
  options:
    syslog-address: "tcp://192.168.0.42:514"
```

## 多实例部署

如果需要运行多个实例进行负载均衡：

```yaml
version: '3.8'

services:
  flow-proxy-1:
    build: .
    ports:
      - "8899:8899"
    volumes:
      - ./secrets.json:/app/secrets.json:ro
    restart: unless-stopped

  flow-proxy-2:
    build: .
    ports:
      - "8900:8899"
    volumes:
      - ./secrets.json:/app/secrets.json:ro
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - flow-proxy-1
      - flow-proxy-2
```

## 参考资料

- [Docker 官方文档](https://docs.docker.com/)
- [Docker Compose 文档](https://docs.docker.com/compose/)
- [Flow Proxy Plugin README](../README.md)
