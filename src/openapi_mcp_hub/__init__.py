"""openapi-mcp-hub: OpenAPI -> MCP 网关。

单端口网关服务：POST 一份 openapi.json 即刻生成一个可接入 agent 的
MCP 端点（Streamable HTTP），运行时把工具调用代理回真实 REST API。
"""

__version__ = "0.5.0"
