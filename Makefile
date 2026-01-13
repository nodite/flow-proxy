.PHONY: help install update setup run test lint format clean docker-build docker-push docker-up docker-down info
.DEFAULT_GOAL := help

# 颜色和项目配置
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m
PROJECT_NAME := flow-proxy-plugin
DOCKER_REGISTRY := docker.io/oscaner
VERSION := $(shell grep '^version = ' pyproject.toml | head -1 | cut -d'"' -f2)

help: ## 显示帮助信息
	@echo "$(BLUE)Flow Proxy Plugin - Makefile 命令$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(NC) %s\n", $$1, $$2}'

# ============================================================================
# 开发环境
# ============================================================================

install: ## 安装项目依赖（含开发依赖）
	@echo "$(BLUE)安装项目依赖...$(NC)"
	@poetry install --with dev

update: ## 更新依赖
	@echo "$(BLUE)更新依赖...$(NC)"
	@poetry update

setup: install ## 初始化项目
	@poetry run pre-commit install
	@[ -f secrets.json ] || (cp secrets.json.template secrets.json && echo "$(RED)请编辑 secrets.json 填入认证信息$(NC)")
	@echo "$(GREEN)✓ 项目初始化完成$(NC)"

# ============================================================================
# 运行和测试
# ============================================================================

run: ## 运行代理服务（开发模式）
	@poetry run flow-proxy --log-level DEBUG

test: ## 运行测试
	@poetry run pytest

test-cov: ## 运行测试并生成覆盖率报告
	@poetry run pytest --cov=flow_proxy_plugin --cov-report=term-missing --cov-report=html
	@echo "$(GREEN)✓ 报告已生成: htmlcov/index.html$(NC)"

# ============================================================================
# 代码质量
# ============================================================================

lint: ## 运行所有代码检查
	@echo "$(BLUE)运行代码检查...$(NC)"
	@poetry run ruff check flow_proxy_plugin tests
	@poetry run mypy flow_proxy_plugin
	@poetry run pylint flow_proxy_plugin

format: ## 格式化代码
	@poetry run ruff format flow_proxy_plugin tests
	@poetry run ruff check --fix flow_proxy_plugin tests
	@echo "$(GREEN)✓ 代码格式化完成$(NC)"

pre-commit: ## 运行 pre-commit 检查
	@poetry run pre-commit run --all-files

# ============================================================================
# Docker 命令
# ============================================================================

docker-build: ## 构建多架构 Docker 镜像
	@echo "$(BLUE)构建多架构镜像...$(NC)"
	@docker build --platform linux/amd64 -t $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 .
	@docker build --platform linux/arm64 -t $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64 .
	@echo "$(GREEN)✓ 镜像构建完成$(NC)"

docker-push: docker-build ## 推送镜像到 Docker Hub
	@echo "$(BLUE)推送镜像...$(NC)"
	@docker push $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64
	@docker push $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64
	@docker manifest create $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION) \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64
	@docker manifest create $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 \
		--amend $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64
	@docker manifest push $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)
	@docker manifest push $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest
	@echo "$(GREEN)✓ 镜像已推送: $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)$(NC)"
	@docker rmi $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-amd64 $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION)-arm64 || true
	@docker manifest rm $(DOCKER_REGISTRY)/$(PROJECT_NAME):$(VERSION) $(DOCKER_REGISTRY)/$(PROJECT_NAME):latest || true

# ============================================================================
# Docker Compose 命令
# ============================================================================

docker-up: ## 启动服务
	@[ -f secrets.json ] || (echo "$(RED)错误: secrets.json 不存在，请先运行: make setup$(NC)" && exit 1)
	@mkdir -p logs
	@docker-compose up -d
	@echo "$(GREEN)✓ 服务已启动 - http://localhost:8899$(NC)"

docker-down: ## 停止服务
	@docker-compose down
	@echo "$(GREEN)✓ 服务已停止$(NC)"

docker-logs: ## 查看日志
	@docker-compose logs -f

docker-restart: ## 重启服务
	@docker-compose restart
	@echo "$(GREEN)✓ 服务已重启$(NC)"

docker-clean: ## 清理 Docker 资源
	@docker-compose down -v --rmi local
	@echo "$(GREEN)✓ 资源已清理$(NC)"

# ============================================================================
# 清理
# ============================================================================

clean: ## 清理临时文件和缓存
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	@echo "$(GREEN)✓ 清理完成$(NC)"

clean-logs: ## 清理日志文件
	@rm -rf logs/*.log *.log
	@echo "$(GREEN)✓ 日志已清理$(NC)"

# ============================================================================
# 快捷命令
# ============================================================================

check: lint test ## 运行所有检查和测试
	@echo "$(GREEN)✓ 所有检查通过$(NC)"

deploy: docker-up ## 快速部署

info: ## 显示项目信息
	@echo "$(BLUE)项目: $(PROJECT_NAME) v$(VERSION)$(NC)"
	@echo "$(BLUE)镜像: $(DOCKER_REGISTRY)/$(PROJECT_NAME)$(NC)"
	@poetry env info
