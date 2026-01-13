# 使用官方 Python 3.12 slim 镜像作为基础镜像
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    FLOW_PROXY_LOG_LEVEL=INFO

# 将 Poetry 添加到 PATH
ENV PATH="$POETRY_HOME/bin:$PATH"

# 安装系统依赖和 Poetry
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -sSL https://install.python-poetry.org | python3 - && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 复制 Poetry 配置文件
COPY pyproject.toml poetry.lock poetry.toml ./

# 安装项目依赖（仅生产依赖）
RUN poetry install --only main --no-root

# 复制项目代码
COPY flow_proxy_plugin ./flow_proxy_plugin
COPY README.md ./

# 安装项目本身
RUN poetry install --only-root

# 创建非 root 用户
RUN useradd -m -u 1000 proxyuser && \
    chown -R proxyuser:proxyuser /app

# 切换到非 root 用户
USER proxyuser

# 暴露默认端口
EXPOSE 8899

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8899/ || exit 1

# 启动命令
ENTRYPOINT ["sh", "-c", "flow-proxy --host 0.0.0.0 --port 8899 --log-level ${FLOW_PROXY_LOG_LEVEL}"]
