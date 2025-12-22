.PHONY: help install dev test lint format clean run docker-build docker-run docker-stop docker-clean docker-logs docker-up docker-down

# 默认目标
.DEFAULT_GOAL := help

# 颜色定义
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

# 项目配置
PROJECT_NAME := flow-proxy-plugin
DOCKER_IMAGE := $(PROJECT_NAME):latest
DOCKER_CONTAINER := flow-proxy
COMPOSE_FILE := docker-compose.yml
DOCKER_REGISTRY := docker.io/oscaner
VERSION = $(shell poetry version -s 2>/dev/null || echo "0.1.0")

help: ## 显示帮助信息
	@echo "$(BLUE)Flow Proxy Plugin - Makefile 命令$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

# ============================================================================
# 开发环境
# ============================================================================

install: ## 安装项目依赖
	@echo "$(BLUE)安装项目依赖...$(NC)"
	poetry install

install-dev: ## 安装开发依赖
	@echo "$(BLUE)安装开发依赖...$(NC)"
	poetry install --with dev

update: ## 更新依赖
	@echo "$(BLUE)更新依赖...$(NC)"
	poetry update

setup: ## 初始化项目（安装依赖 + pre-commit）
	@echo "$(BLUE)初始化项目...$(NC)"
	poetry install --with dev
	poetry run pre-commit install
	@if [ ! -f secrets.json ]; then \
		echo "$(YELLOW)创建配置文件...$(NC)"; \
		cp secrets.json.template secrets.json; \
		echo "$(RED)请编辑 secrets.json 填入认证信息$(NC)"; \
	fi
	@echo "$(GREEN)✓ 项目初始化完成$(NC)"

# ============================================================================
# 运行和测试
# ============================================================================

run: ## 运行代理服务（开发模式）
	@echo "$(BLUE)启动代理服务...$(NC)"
	poetry run flow-proxy --log-level DEBUG

run-prod: ## 运行代理服务（生产模式）
	@echo "$(BLUE)启动代理服务（生产模式）...$(NC)"
	poetry run flow-proxy --host 0.0.0.0 --port 8899 --log-level INFO

test: ## 运行测试
	@echo "$(BLUE)运行测试...$(NC)"
	poetry run pytest

test-cov: ## 运行测试并生成覆盖率报告
	@echo "$(BLUE)运行测试（带覆盖率）...$(NC)"
	poetry run pytest --cov=flow_proxy_plugin --cov-report=term-missing

test-cov-html: ## 生成 HTML 覆盖率报告
	@echo "$(BLUE)生成 HTML 覆盖率报告...$(NC)"
	poetry run pytest --cov=flow_proxy_plugin --cov-report=html
	@echo "$(GREEN)✓ 报告已生成: htmlcov/index.html$(NC)"

test-watch: ## 监视模式运行测试
	@echo "$(BLUE)监视模式运行测试...$(NC)"
	poetry run pytest-watch

# ============================================================================
# 代码质量
# ============================================================================

lint: ## 运行所有代码检查
	@echo "$(BLUE)运行代码检查...$(NC)"
	@$(MAKE) lint-ruff
	@$(MAKE) lint-mypy
	@$(MAKE) lint-pylint

lint-ruff: ## 运行 Ruff 检查
	@echo "$(BLUE)Ruff 检查...$(NC)"
	poetry run ruff check flow_proxy_plugin tests

lint-mypy: ## 运行 MyPy 类型检查
	@echo "$(BLUE)MyPy 类型检查...$(NC)"
	poetry run mypy flow_proxy_plugin

lint-pylint: ## 运行 Pylint 检查
	@echo "$(BLUE)Pylint 检查...$(NC)"
	poetry run pylint flow_proxy_plugin

format: ## 格式化代码
	@echo "$(BLUE)格式化代码...$(NC)"
	poetry run ruff format flow_proxy_plugin tests
	poetry run ruff check --fix flow_proxy_plugin tests
	@echo "$(GREEN)✓ 代码格式化完成$(NC)"

format-check: ## 检查代码格式（不修改）
	@echo "$(BLUE)检查代码格式...$(NC)"
	poetry run ruff format --check flow_proxy_plugin tests

pre-commit: ## 运行 pre-commit 检查
	@echo "$(BLUE)运行 pre-commit 检查...$(NC)"
	poetry run pre-commit run --all-files

# ============================================================================
# Docker 命令
# ============================================================================

docker-build: ## 构建多架构 Docker 镜像（AMD64 + ARM64）
	@echo "$(BLUE)构建多架构镜像...$(NC)"
	docker build --platform linux/amd64 -t $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 .
	docker build --platform linux/arm64 -t $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64 .
	@echo "$(GREEN)✓ 多架构镜像构建完成: $(DOCKER_REGISTRY)/$(PROJECT_NAME)$(NC)"

docker-push: ## 推送多架构镜像到 Docker Hub 并清理本地
	@echo "$(BLUE)推送多架构镜像到 Docker Hub...$(NC)"
	docker push $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64
	docker push $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64
	@echo "$(BLUE)创建并推送 manifest...$(NC)"
	docker manifest create $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION) \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64
	docker manifest create $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64
	docker manifest push $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)
	docker manifest push $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest
	@echo "$(GREEN)✓ 镜像已推送到: $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)$(NC)"
	@echo "$(GREEN)✓ 镜像已推送到: $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest$(NC)"
	@echo "$(BLUE)清理本地镜像和 manifest...$(NC)"
	docker rmi $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 || true
	docker rmi $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64 || true
	docker manifest rm $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION) || true
	docker manifest rm $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest || true
	@echo "$(GREEN)✓ 本地资源已清理$(NC)"

docker-run: ## 运行 Docker 容器
	@echo "$(BLUE)运行 Docker 容器...$(NC)"
	@if [ ! -f secrets.json ]; then \
		echo "$(RED)错误: secrets.json 不存在$(NC)"; \
		echo "$(YELLOW)请先运行: make setup$(NC)"; \
		exit 1; \
	fi
	docker run -d \
		--name $(DOCKER_CONTAINER) \
		-p 8899:8899 \
		-v $(PWD)/secrets.json:/app/secrets.json:ro \
		-v $(PWD)/logs:/app/logs \
		$(DOCKER_IMAGE)
	@echo "$(GREEN)✓ 容器已启动: $(DOCKER_CONTAINER)$(NC)"

docker-stop: ## 停止 Docker 容器
	@echo "$(BLUE)停止 Docker 容器...$(NC)"
	docker stop $(DOCKER_CONTAINER) || true
	docker rm $(DOCKER_CONTAINER) || true
	@echo "$(GREEN)✓ 容器已停止$(NC)"

docker-logs: ## 查看 Docker 容器日志
	@echo "$(BLUE)查看容器日志...$(NC)"
	docker logs -f $(DOCKER_CONTAINER)

docker-shell: ## 进入 Docker 容器
	@echo "$(BLUE)进入容器 shell...$(NC)"
	docker exec -it $(DOCKER_CONTAINER) /bin/bash

docker-clean: ## 清理 Docker 资源
	@echo "$(BLUE)清理 Docker 资源...$(NC)"
	docker stop $(DOCKER_CONTAINER) || true
	docker rm $(DOCKER_CONTAINER) || true
	docker rmi $(DOCKER_IMAGE) $(DOCKER_IMAGE)-amd64 $(DOCKER_IMAGE)-arm64 || true
	@echo "$(GREEN)✓ Docker 资源已清理$(NC)"

# ============================================================================
# Docker Compose 命令
# ============================================================================

docker-up: ## 启动 Docker Compose 服务
	@echo "$(BLUE)启动 Docker Compose 服务...$(NC)"
	@if [ ! -f secrets.json ]; then \
		echo "$(RED)错误: secrets.json 不存在$(NC)"; \
		echo "$(YELLOW)请先运行: make setup$(NC)"; \
		exit 1; \
	fi
	@mkdir -p logs
	docker-compose up -d
	@echo "$(GREEN)✓ 服务已启动$(NC)"
	@echo "$(YELLOW)查看日志: make docker-compose-logs$(NC)"

docker-down: ## 停止 Docker Compose 服务
	@echo "$(BLUE)停止 Docker Compose 服务...$(NC)"
	docker-compose down
	@echo "$(GREEN)✓ 服务已停止$(NC)"

docker-compose-build: ## 构建 Docker Compose 服务
	@echo "$(BLUE)构建 Docker Compose 服务...$(NC)"
	docker-compose build
	@echo "$(GREEN)✓ 构建完成$(NC)"

docker-compose-logs: ## 查看 Docker Compose 日志
	@echo "$(BLUE)查看服务日志...$(NC)"
	docker-compose logs -f

docker-compose-ps: ## 查看 Docker Compose 服务状态
	@echo "$(BLUE)服务状态:$(NC)"
	docker-compose ps

docker-compose-restart: ## 重启 Docker Compose 服务
	@echo "$(BLUE)重启服务...$(NC)"
	docker-compose restart
	@echo "$(GREEN)✓ 服务已重启$(NC)"

docker-compose-clean: ## 清理 Docker Compose 资源
	@echo "$(BLUE)清理 Docker Compose 资源...$(NC)"
	docker-compose down -v --rmi local
	@echo "$(GREEN)✓ 资源已清理$(NC)"

# ============================================================================
# 清理
# ============================================================================

clean: ## 清理临时文件和缓存
	@echo "$(BLUE)清理临时文件...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	rm -rf dist build
	@echo "$(GREEN)✓ 清理完成$(NC)"

clean-logs: ## 清理日志文件
	@echo "$(BLUE)清理日志文件...$(NC)"
	rm -f *.log
	rm -rf logs/*.log
	@echo "$(GREEN)✓ 日志已清理$(NC)"

clean-all: clean clean-logs docker-compose-clean ## 清理所有文件和 Docker 资源
	@echo "$(GREEN)✓ 所有资源已清理$(NC)"

# ============================================================================
# 快捷命令
# ============================================================================

dev: install-dev ## 开发环境快速设置
	@echo "$(GREEN)✓ 开发环境已就绪$(NC)"

check: lint test ## 运行所有检查和测试
	@echo "$(GREEN)✓ 所有检查通过$(NC)"

deploy: docker-compose-build docker-up ## 快速部署（Docker Compose）
	@echo "$(GREEN)✓ 部署完成$(NC)"
	@echo "$(YELLOW)服务地址: http://localhost:8899$(NC)"

restart: docker-compose-restart ## 快速重启服务
	@echo "$(GREEN)✓ 服务已重启$(NC)"

status: docker-compose-ps ## 查看服务状态
	@echo ""

logs: docker-compose-logs ## 查看日志

# ============================================================================
# 信息显示
# ============================================================================

info: ## 显示项目信息
	@echo "$(BLUE)项目信息:$(NC)"
	@echo "  名称: $(PROJECT_NAME)"
	@echo "  镜像: $(DOCKER_IMAGE)"
	@echo "  容器: $(DOCKER_CONTAINER)"
	@echo ""
	@echo "$(BLUE)Python 环境:$(NC)"
	@poetry env info
	@echo ""
	@echo "$(BLUE)依赖信息:$(NC)"
	@poetry show --tree --only main | head -n 10
