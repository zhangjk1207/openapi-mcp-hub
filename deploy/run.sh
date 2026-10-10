#!/usr/bin/env bash
# 常驻启动 openapi-mcp-hub 网关（默认 0.0.0.0:20033）
#
# 用法：
#   nohup deploy/run.sh >/dev/null 2>&1 &     # 后台常驻
#   ADMIN_TOKEN=xxx nohup deploy/run.sh &     # 指定管理 token
#
# 首次运行自动生成随机管理 token 并保存到 deploy/admin_token（已 gitignore）。
# 持久化：默认文件后端（data/services/*.json）；存在 deploy/pg.env 时切换 PostgreSQL
# （首次启动自动把文件记录导入 pg，原文件保留可回滚）。
# 目标 API 的凭证（如 DEMO_PETSTORE_TOKEN）需在启动前 export，网关只引用变量名。
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN_FILE="deploy/admin_token"
mkdir -p deploy logs data
if [ -z "${ADMIN_TOKEN:-}" ]; then
    if [ -f "$TOKEN_FILE" ]; then
        ADMIN_TOKEN="$(cat "$TOKEN_FILE")"
    else
        ADMIN_TOKEN="a2m-$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(16))')"
    fi
fi
umask 077
printf '%s' "$ADMIN_TOKEN" > "$TOKEN_FILE"

# 可选：加载 PostgreSQL 配置（deploy/pg.env，已 gitignore）
PG_ENV_FILE="deploy/pg.env"
if [ -f "$PG_ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$PG_ENV_FILE"
    set +a
fi

echo "[$(date '+%F %T')] 启动网关 admin_token=$ADMIN_TOKEN" >> logs/gateway.log

exec env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    OPENAPI_MCP_HUB_ADMIN_TOKEN="$ADMIN_TOKEN" \
    OPENAPI_MCP_HUB_DATA_DIR="$PWD/data" \
    OPENAPI_MCP_HUB_HOST="${OPENAPI_MCP_HUB_HOST:-0.0.0.0}" \
    OPENAPI_MCP_HUB_PORT="${OPENAPI_MCP_HUB_PORT:-20033}" \
    PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}" \
    .venv/bin/python -m openapi_mcp_hub >> logs/gateway.log 2>&1
