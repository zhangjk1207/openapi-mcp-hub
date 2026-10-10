#!/usr/bin/env bash
# 启动演示用 Petstore mock（默认 127.0.0.1:20034），模拟被网关代理的"真实"目标 API。
# 用法：nohup deploy/run-mock.sh >/dev/null 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
exec env DEMO_PETSTORE_TOKEN="${DEMO_PETSTORE_TOKEN:-demo-secret-token}" \
    .venv/bin/python demo/petstore_mock.py "${PORT:-20034}" >> logs/petstore-mock.log 2>&1
