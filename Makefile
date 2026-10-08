# choyeon-panel 常用操作入口
# 所有变量均可用环境变量覆盖：CP_PREFIX / CP_PORT / CP_DATA_DIR / CP_BACKUP_DIR

SHELL := /usr/bin/env bash
PREFIX ?= /root/choyeon-panel
PORT ?= 3210
SCRIPT := $(PREFIX)/scripts

.DEFAULT_GOAL := help
.PHONY: help install update backup health status restart stop logs \
        backend-dev web-dev build build-web test lint fmt clean release-dry

help: ## 显示帮助
	@printf 'choyeon-panel 常用命令\n\n'
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

# ---------- 部署 ----------
install: ## 安装到 $(PREFIX)（可加 CP_SKIP_NGINX=1）
	CP_PREFIX=$(PREFIX) CP_PORT=$(PORT) bash scripts/install.sh

update: ## 升级已安装实例（失败自动回滚）
	CP_PREFIX=$(PREFIX) bash scripts/update.sh

backup: ## 备份数据库与 .env
	CP_PREFIX=$(PREFIX) bash scripts/backup.sh

health: ## 健康检查
	CP_PREFIX=$(PREFIX) bash scripts/healthcheck.sh

status: ## 查看服务状态
	systemctl status choyeon-panel --no-pager -l || true

restart: ## 重启服务
	systemctl restart choyeon-panel && systemctl is-active choyeon-panel

stop: ## 停止服务
	systemctl stop choyeon-panel

logs: ## 跟踪日志
	journalctl -u choyeon-panel -f

# ---------- 开发 ----------
backend-dev: ## 本地启动后端（3210）
	cd backend && CP_DATA_DIR=$$(pwd)/data python3 -m app.main

web-dev: ## 本地启动前端（5173，代理 /api 到 3210）
	cd web && npm run dev

build: build-web ## 构建前端产物
build-web: ## 仅构建前端
	cd web && npm run build

test: ## 后端单元测试（用 .venv；测试依赖见 backend/requirements-dev.txt）
	cd backend && .venv/bin/python -m unittest discover -s tests -v

lint: ## 静态检查（ruff + mypy + vue-tsc）
	cd backend && (ruff check . || true) && (mypy app || true)
	cd web && npm run typecheck

fmt: ## 格式化（ruff）
	cd backend && ruff format . || ruff check --fix .

clean: ## 清理构建产物与缓存
	rm -rf web/dist backend/.venv backend/data
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

release-dry: ## 本地模拟发布打包（不推送）
	cd web && npm run build
	tar --exclude='.git' --exclude=node_modules --exclude=.venv --exclude=data \
		-czf /tmp/choyeon-panel-dev.tar.gz .
	@ls -lh /tmp/choyeon-panel-dev.tar.gz
