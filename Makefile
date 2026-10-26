# 开发者快捷入口。规则本身固化在 pyproject.toml 的 [tool.ruff] 里，
# 因此本地 `make lint` 与 CI 跑的是同一套标准。

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help install test lint fmt check build clean

help: ## 列出可用目标
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /: /'

install: ## 创建 .venv 并安装（含 dev 依赖与 ruff）
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e ".[dev]" ruff

test: ## 跑单测（pg/mysql 集成用例缺 DSN 时自动跳过）
	$(BIN)/pytest tests/ -q

lint: ## ruff 检查
	$(BIN)/ruff check src tests

fmt: ## ruff 自动修复
	$(BIN)/ruff check --fix src tests

check: lint test ## 与 CI 同款：lint + test

build: ## 构建 sdist + wheel 并校验元数据
	$(BIN)/pip install -U build twine
	$(BIN)/python -m build
	$(BIN)/python -m twine check --strict dist/*

clean: ## 清理构建与缓存产物
	rm -rf dist build .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +
