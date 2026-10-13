"""数据模型：注册 payload（pydantic）与流水线中间模型（dataclass）。

中间模型刻意与 pydantic 解耦：流水线（loader→extractor→generator）
用轻量 dataclass，只在管理面边界做请求校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------- 注册 payload

SERVICE_NAME_PATTERN = r"^[a-z][a-z0-9-]{1,31}$"


class AuthConfig(BaseModel):
    """目标 API 认证：只引用环境变量名，凭证永不落盘。"""

    type: Literal["bearer", "api_key", "basic", "oauth2"] = "bearer"
    # bearer
    token_env: str | None = None
    # api_key
    key_name: str = "X-API-Key"
    location: Literal["header", "query"] = "header"
    value_env: str | None = None
    # basic
    user_env: str | None = None
    password_env: str | None = None
    # oauth2（client_credentials 模式）：token_url 为注册期静态配置，
    # client_id/client_secret 只经环境变量引用；token 由网关代取并缓存（见 invoker）
    token_url: str | None = None
    client_id_env: str | None = None
    client_secret_env: str | None = None
    # scope：直接写字面量，或引用环境变量（scope_env 优先）；均可不填
    scope: str | None = None
    scope_env: str | None = None

    @model_validator(mode="after")
    def _oauth2_requires(self) -> "AuthConfig":
        if self.type == "oauth2":
            missing = [
                label
                for label, v in (
                    ("token_url", self.token_url),
                    ("client_id_env", self.client_id_env),
                    ("client_secret_env", self.client_secret_env),
                )
                if not v
            ]
            if missing:
                raise ValueError(f"oauth2 认证缺少必填字段: {', '.join(missing)}")
            if not str(self.token_url).startswith(("http://", "https://")):
                raise ValueError("token_url 必须以 http(s):// 开头")
        return self


class Filters(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    include_deprecated: bool = False


class ServiceRegistration(BaseModel):
    """POST/PUT /services 请求体。"""

    name: str = Field(pattern=SERVICE_NAME_PATTERN)
    title: str | None = None
    # spec 原文（dict）/ spec 文本（JSON 或 YAML 字符串，常来自上传的文件）/ 可拉取的 URL，三选一
    spec: dict[str, Any] | None = None
    spec_text: str | None = None
    spec_url: str | None = None
    base_url: str | None = None
    auth: AuthConfig | None = None
    filters: Filters = Field(default_factory=Filters)
    max_tools: int | None = Field(default=None, ge=1)
    timeout_s: float = Field(default=30.0, gt=0, le=300)
    owner: str = ""
    # 响应模板：服务级默认，对所有工具生效；不填则 passthrough 原始 JSON
    response_template: str | None = None
    # 工具级覆盖：{工具名: 模板}，优先于服务级默认
    tool_response_templates: dict[str, str] = Field(default_factory=dict)
    # 运行时工具白名单：只暴露列出的工具（提取后的子集，不改 spec）；空 = 全部暴露
    allowed_tools: list[str] = Field(default_factory=list)
    # 数据库源（M5）：与 spec/spec_url 二选一，注册后内省库表生成查询工具
    db: "DbSourceConfig | None" = None

    @model_validator(mode="after")
    def _spec_xor_db(self) -> "ServiceRegistration":
        sources = [self.spec, self.spec_text, self.spec_url]
        if sum(1 for s in sources if s is not None) > 1:
            raise ValueError("spec / spec_text / spec_url 互斥，只能填一个")
        if self.db is not None and any(s is not None for s in sources):
            raise ValueError("db 与 spec/spec_text/spec_url 互斥，只能二选一")
        return self


class DbSourceConfig(BaseModel):
    """数据库注册源：PostgreSQL 只读（M5-1，安全设计见 docs/PLAN-DB-MCP.md §5）。

    凭证只引用环境变量名（DSN 含密码，永不落盘/入日志）；执行侧只读三重强制：
    只读事务 + 语句白名单（首 token 仅 SELECT/WITH/EXPLAIN）+ 建议运维配只读账号。
    """

    # DSN 环境变量名，如 WORKFLOW_DB_URL=postgresql://user:pass@host:5432/dbname
    url_env: str
    # 对外字段名 schema（alias），避免遮蔽 BaseModel.schema；落盘键为 db_schema
    db_schema: str = Field(default="public", alias="schema")
    model_config = ConfigDict(populate_by_name=True)
    mode: Literal["readonly"] = "readonly"  # 写模式未实现（M5-3 另行评审）
    tool_style: Literal["general"] = "general"  # per_table 未实现（M5-2）
    # 表过滤：fnmatch 通配；默认额外排除系统模式下的表
    include_tables: list[str] = Field(default_factory=list)
    exclude_tables: list[str] = Field(default_factory=list)
    max_rows: int = 200  # 单次查询返回行数上限，超出截断并标记 truncated
    timeout_s: float = 15.0  # 语句超时（statement_timeout，池级注入）


# ---------------------------------------------------------------- 流水线中间模型


class ParamLocation(str, Enum):
    path = "path"
    query = "query"
    header = "header"
    cookie = "cookie"


@dataclass
class ApiParam:
    name: str
    location: ParamLocation
    required: bool = False
    schema: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class ApiBody:
    schema: dict[str, Any] = field(default_factory=dict)
    required: bool = False
    description: str = ""
    media_type: str = "application/json"


@dataclass
class ApiEndpoint:
    method: str  # 小写 http method
    path: str
    operation_id: str | None = None
    summary: str = ""
    description: str = ""
    deprecated: bool = False
    tags: list[str] = field(default_factory=list)
    params: list[ApiParam] = field(default_factory=list)
    body: ApiBody | None = None


@dataclass
class ToolDef:
    """extractor/generator 产物：一个接口对应一个 MCP 工具。

    input_schema 为最终暴露给 LLM 的 JSON Schema；endpoint 保留原始
    位置信息，供 invoker 还原 HTTP 请求。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    # REST 源：保留原始位置信息，供 invoker 还原 HTTP 请求
    endpoint: ApiEndpoint | None = None
    # DB 源：动作名（list_tables / describe_table / query），由 db executor 执行
    db_action: str | None = None
    # 由 registry 在注册时挂载（服务级默认 + 工具级覆盖），运行时 mcp_host 渲染
    response_template: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "responseTemplate": self.response_template,
        }
