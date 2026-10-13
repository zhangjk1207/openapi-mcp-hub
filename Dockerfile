# openapi-mcp-hub 网关镜像：python:3.12-slim + pip install .
# 数据面单端口 20033；管理面 token 与目标 API 凭证全部经环境变量注入（凭证永不落盘）
# 国内/内网构建：docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

# 先装依赖层（利用 Docker 缓存：改代码不重拉依赖）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} .

# demo mock（compose 演示编排用；不用不影响网关）
COPY demo ./demo

# 运行时配置：0.0.0.0 才能被容器外访问；数据目录挂卷以持久化已注册服务
ENV OPENAPI_MCP_HUB_HOST=0.0.0.0 \
    OPENAPI_MCP_HUB_PORT=20033 \
    PYTHONUNBUFFERED=1
EXPOSE 20033
VOLUME ["/app/data"]

# 健康检查：/healthz 不需要任何 token/凭证；slim 镜像无 curl，用标准库
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:20033/healthz', timeout=3)"]

CMD ["python", "-m", "openapi_mcp_hub"]
