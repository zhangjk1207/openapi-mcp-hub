# api-to-mcp 网关镜像：python:3.12-slim + pip install .
# 数据面单端口 20033；管理面 token 与目标 API 凭证全部经环境变量注入（凭证永不落盘）
FROM python:3.12-slim

WORKDIR /app

# 先装依赖层（利用 Docker 缓存：改代码不重拉依赖；内网环境用清华镜像源）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

# demo mock（可选编排用；不用不影响网关）
COPY demo ./demo

# 运行时配置：0.0.0.0 才能被容器外访问；数据目录挂卷以持久化已注册服务
ENV API_TO_MCP_HOST=0.0.0.0 \
    API_TO_MCP_PORT=20033 \
    PYTHONUNBUFFERED=1
EXPOSE 20033
VOLUME ["/app/data"]

CMD ["python", "-m", "api_to_mcp"]
