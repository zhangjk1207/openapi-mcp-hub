# openapi-mcp-hub

**OpenAPI → MCP 网关**：起一个服务（如 `localhost:20033`），向它 POST 一份 `openapi.json`，立刻得到一个可直接接入 agent 的 MCP 端点 —— `http://localhost:20033/mcp/{service-name}`。

**也可以直接注册一个 PostgreSQL 库**（无需写接口）：内省库表 → 只读查询工具，同一网关同一套 key/观测。

单端口、路径级多租户、注册即热生效；不为每份 spec 单独部署进程、单独占端口。

浏览器打开网关根路径即是**内置控制台**（单文件静态页，零构建链零外部依赖）：表单注册
（含预检）、服务列表/详情、健康探测、调用日志、key 轮换——内网环境开箱即用。

> ✅ 当前状态（v0.2.0）：**M4 + 持久化 + DB→MCP（M5-1）已交付** —— SSE 兼容端点、健康探测、Docker 化、PostgreSQL 双后端持久化（自动迁移）、数据库只读三件套工具（PostgreSQL + MySQL 8）；164 项测试全过。English docs: [README.md](README.md)
>
> 📖 **怎么用看 [USAGE.md](USAGE.md)**：注册方接入指南（REST + 数据库）+ agent 使用方接入指南 + 报错速查表。

![demo](docs/demo.gif)

---

## 1. 背景与目标

- **OpenAPI（Swagger）** 是 REST 接口描述的事实标准，内部大量服务都有现成的 `openapi.json`；
- **MCP（Model Context Protocol）** 已成为 agent 接入工具的主流协议，Claude Code 等客户端原生支持 Streamable HTTP 传输；
- 手工为每个接口包装 MCP tool 重复且难维护；而"每份 spec 部署一个 MCP 服务"会造成端口泛滥、运维成本线性增长。

因此本项目的形态是**网关（Gateway）**：常驻一个服务进程，注册多少份 spec，就动态挂载多少个 MCP 端点，全部收敛在同一个端口下。

## 2. 架构

```
            ┌──────────── 管理面（admin token 鉴权）─────────────┐
            │  POST   /services            注册新服务            │
            │  GET    /services            服务列表（健康/工具数）│
            │  GET    /services/{name}/tools   工具清单预览       │
            │  PUT    /services/{name}     更新 spec / 配置      │
            │  DELETE /services/{name}     下线                  │
            └───────────────┬────────────────────────────────────┘
                            │ 原子落盘 data/services/*.json（tmp+rename）
                            ▼
┌───────────── Gateway：FastAPI + 每服务一个 MCP Server 实例 ─────────────┐
│ 数据面：POST /mcp/{name}（Streamable HTTP，per-service key）        │
│ 兼容：GET /mcp/{name}/sse + POST /mcp/{name}/message（老客户端）  │
│ Registry（读写锁，启动全量恢复）                                     │
│ Loader → Extractor → Generator → 动态挂载 / 卸载                    │
│ Invoker：per-service httpx client + 超时 + 熔断 + 限流              │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
                        各目标 REST API
```

- **管理面** `/services/*`：面向注册方（人或 CI），负责服务生命周期；
- **数据面** `/mcp/{name}`：面向 agent 客户端，MCP Streamable HTTP 端点，Claude Code 直接填 URL 即可接入；
- **SSE 兼容** `/mcp/{name}/sse`（GET）+ `/mcp/{name}/message?session_id=...`（POST）：老客户端兼容通道，与 streamable 共享同一套工具回调与鉴权，双通道可同时使用；
- 两个命名空间物理隔离，服务名不可能与管理路由冲突（对比 `/{name}/mcp` 方案：注册一个叫 `add` 的服务就会撞路由）。

## 3. 三十秒上手（规划中的完整流程）

```bash
# ① 启动网关（一次）
export OPENAPI_MCP_HUB_ADMIN_TOKEN=<管理token>
python -m openapi_mcp_hub          # 监听 0.0.0.0:20033（OPENAPI_MCP_HUB_HOST/PORT 可改；容器化见 M3）

# ② 注册一个服务：spec + 真实地址 + 凭证引用（注意：光有 openapi.json 不够）
curl -X POST http://localhost:20033/services \
  -H "Authorization: Bearer $OPENAPI_MCP_HUB_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @examples/register-petstore.json

# ③ 响应直接给出接入命令（注册即热生效，无需重启）
# {
#   "name": "petstore",
#   "mcp_url": "http://localhost:20033/mcp/petstore",
#   "api_key": "psk-xxxx（仅此一次返回，可轮换）",
#   "tools": ["list_pets", "get_pet_by_id", "create_pet"],
#   "claude_code_cmd": "claude mcp add --transport http petstore http://localhost:20033/mcp/petstore --header 'X-Api-Key: psk-xxxx'"
# }

# ④ 接入 Claude Code
claude mcp add --transport http petstore http://localhost:20033/mcp/petstore \
  --header "X-Api-Key: psk-xxxx"
```

之后 agent 中的 LLM 调用工具 → 网关把参数还原成真实 HTTP 请求转发到目标 API → 结构化结果返回给 LLM。

**本地开发与测试**：

```bash
pip install -e ".[dev]"     # 或在共享 venv 中安装 pytest/pytest-asyncio/respx
python -m pytest tests/      # 78 个用例：单测 + respx mock + e2e（真实 uvicorn 双服务 + MCP 客户端）
```

适配说明：基于 mcp SDK **2.x**（2.3+）——lowlevel `Server` 构造器注入 `on_list_tools`/`on_call_tool` 回调、types 为 snake_case、入参校验由网关用 jsonschema 按 inputSchema 自行执行（2.x 不再内置校验）。运行环境使用项目独立 venv（`python -m venv .venv && .venv/bin/pip install -e ".[dev]"`），不影响机器上其他项目的共享环境。

### 60 秒体验（Docker 一键编排）

```bash
git clone https://github.com/zhangjk1207/openapi-mcp-hub && cd openapi-mcp-hub
docker compose up -d       # 网关 + PostgreSQL + 演示 API，自动注册 petstore 演示服务
```

打开 **http://localhost:20033**，输入演示管理 token：`demo-admin-token` —— 控制台里已有一个可直接使用的 `petstore` 服务。立刻体验 agent 可调用的端点：

```bash
curl -X POST http://localhost:20033/mcp/petstore \
  -H "X-Api-Key: psk-demo-petstore-key" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_pets","arguments":{"limit":3}}}'
```

接入 Claude Code：

```bash
claude mcp add --transport http petstore http://localhost:20033/mcp/petstore \
  --header "X-Api-Key: psk-demo-petstore-key"
```

> 演示模式（compose 里的 `OPENAPI_MCP_HUB_DEMO=1`）使用固定公开的演示 key，只为零摩擦上手。正式使用前请关闭演示模式并修改 `OPENAPI_MCP_HUB_ADMIN_TOKEN`——真实注册永远返回一次性随机 key。

### 部署（常驻，无 Docker）

```bash
# ① 启动网关（0.0.0.0:20033；首次自动生成并保存管理 token 到 deploy/admin_token）
nohup deploy/run.sh >/dev/null 2>&1 &

# ② （可选）启动演示目标 API（127.0.0.1:20034，带 Bearer 认证的 petstore mock）
nohup deploy/run-mock.sh >/dev/null 2>&1 &

# ③ 用 examples/register-petstore.json 注册演示服务
ADMIN_TOKEN=$(cat deploy/admin_token)
curl -s -X POST http://127.0.0.1:20033/services \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d @examples/register-petstore.json | python3 -m json.tool
```

停止：`pkill -f 'openapi_mcp_hub'`（网关）/ `pkill -f petstore_mock`（mock）；日志在 `logs/`。

### 持久化：文件或 PostgreSQL

默认文件后端（`data/services/*.json` 原子写，重启自动恢复）。切 PostgreSQL：

```bash
cp deploy/pg.env.example deploy/pg.env   # 填入 DSN（密码特殊字符需 URL 转义）
DEMO_PETSTORE_TOKEN=xxx nohup deploy/run.sh ...   # 重启即生效
```

- 表结构：`services(name PK, title, base_url, api_key_hash, owner, created_at, updated_at, record JSONB)`，record 与文件模式同构；
- **首次切换自动迁移**：pg 表为空且 data/services 有记录 → 导入（原文件保留，去掉 pg.env 重启即回滚到文件模式；
  注意迁移是单向的：pg 期间新注册的服务不回写文件，回滚会回到迁移时点）；
- pg 不可达时网关启动失败退出（fail fast，由 supervisor/容器重启重试）；
- 集成测试：`OMH_PG_TEST_DSN=... pytest tests/test_store_pg.py`（未配置自动跳过）。

### 数据库 → MCP（M5-1）：不用写接口，直接查库

没有 REST API 的数据，直接把库注册进来（**PostgreSQL / MySQL 8+ 只读**，方言由 DSN 前缀自动推断）——`db` 与 `spec` 二选一：

```bash
# ① DSN 含密码只经环境变量引用（永不落盘；与目标 API 凭证同原则）
export WORKFLOW_DB_URL='postgresql://ro_user:***@db.internal.example:5432/workflow'   # MySQL：mysql://user:***@host:3306/db

# ② 注册：内省库表 → 生成三件套工具（工具数恒定，表多不爆炸）
curl -X POST http://localhost:20033/services -H "Authorization: Bearer $ADMIN_TOKEN" -d '{
  "name": "workflow-db",
  "db": {"url_env": "WORKFLOW_DB_URL", "schema": "public",
         "exclude_tables": ["*_audit"], "max_rows": 200}
}'
# → 工具：list_tables / describe_table / query（schema 摘要已写进 query 描述，LLM 可盲写 SQL）
```

安全六层（详见 docs/PLAN-DB-MCP.md）：DSN 环境变量引用、只读三重强制（READ ONLY 事务 +
语句白名单 + 建议只读账号）、语句超时（pg：statement_timeout / mysql：max_execution_time）/
行数上限（外层 LIMIT 包裹）、独立连接池、表过滤、SQL 摘要进调用日志。写操作一律拦截（-32602）。

与 REST 服务同窗管理：同一个 `/mcp/{name}`、同一套 key/鉴权/持久化/健康探测/观测。

### Docker 部署（M4）

```bash
# 构建（内网环境用 host 网络直连清华 pypi 镜像）
docker build --network host -t openapi-mcp-hub:latest .

# 运行（管理 token 与目标 API 凭证全部经 env 注入，凭证不落盘；data 卷持久化已注册服务）
docker run -d --network host \
  -e OPENAPI_MCP_HUB_ADMIN_TOKEN=$ADMIN_TOKEN \
  -e GRANDE_TOKEN=xxx \   # 按各服务注册时的 *_env 名注入目标 API 凭证
  -v $PWD/data:/app/data openapi-mcp-hub:latest

# 或 docker compose（含可选 petstore 演示目标）
OPENAPI_MCP_HUB_ADMIN_TOKEN=xxx docker compose up -d
```

镜像验证过的能力：容器内注册/数据卷重启恢复、health 凭证自检（缺 env 时 `auth_env_ok:false`）、streamable 与 SSE 双通道全链路。

## 4. 关键决策记录（已定）

| 决策点 | 结论 | 说明 |
|---|---|---|
| 管理面鉴权 | 静态 admin token | `OPENAPI_MCP_HUB_ADMIN_TOKEN` 环境变量注入，内网场景够用 |
| 目标 API 凭证 | 网关代持 | 注册时只引用**环境变量名**（如 `GRANDE_TOKEN`），凭证永不落盘 |
| 数据面鉴权 | 每服务独立 key | 注册时生成、仅返回一次、落盘存哈希、支持轮换；请求头 `X-Api-Key` |
| 首批客户端 | Claude Code | 原生支持 Streamable HTTP；Cursor / Cline / LangGraph 同样可用；Claude Desktop 需 mcp-remote 桥（M4 提供模板） |

## 5. 管理面 API

| 方法 | 路径 | 作用 | 说明 |
|---|---|---|---|
| POST | `/services` | 注册 | 解析 → 生成工具 → 挂载 → 落盘，秒级同步返回；同名返回 409，覆盖必须显式 PUT |
| GET | `/services` | 列表 | 含健康状态、工具数、owner、更新时间 |
| POST | `/services/preview` | 预览 | 完整走流水线但不落盘、不生成端点/key；可选 `sample_response` 直接渲染模板效果（对应 Unla template-tester） |
| GET | `/services/{name}` | 详情 | 配置 + 工具清单 + 目标 API 健康探测结果 |
| GET | `/services/{name}/health` | 健康探测 | 对目标 API 发 HEAD（405 时回退 GET），返回 reachable/状态码/延迟 ms；顺带自检凭证环境变量是否已配置（不回传值） |
| GET | `/services/{name}/calls` | 调用日志 | 近期 200 条工具调用（内存环形缓冲，重启清零）：工具/状态/耗时/错误码 |
| PUT | `/services/{name}` | 更新 | 替换 spec / base_url / 过滤规则；**更新即作废该服务所有 MCP session**（客户端自动重连重新 initialize） |
| DELETE | `/services/{name}` | 下线 | 卸载路由 + 删除落盘；agent 侧表现为连接失败 |
| POST | `/services/{name}/rotate-key` | 轮换 | 重新生成数据面 key |

**注册请求体**：

```json
{
  "name": "grande-basic",
  "title": "格兰德-企业基本信息",
  "spec": { "...": "openapi.json 内容，或用 spec_url 让网关拉取" },
  "base_url": "http://grande.internal:8080",
  "auth": { "type": "bearer", "token_env": "GRANDE_TOKEN" },
  "filters": { "include": ["/api/**"], "exclude_tags": ["internal"] },
  "max_tools": 100,
  "owner": "zhangjikang"
}
```

字段说明：

- `name`：必填，`^[a-z][a-z0-9-]{1,31}$`，**注册后不可改**（agent 配置里写死了 URL）；`title` 仅展示——内部 spec 标题多为中文，不做自动命名
- `spec` / `spec_url`：二选一；JSON/YAML 均可，OpenAPI 3.0/3.1
- `base_url`：目标 API 真实地址；缺省取 spec 的 `servers[0]` 并返回 warning（spec 里的 servers 经常不可信）
- `auth`：`bearer` / `api_key`（header 或 query）/ `basic`；只存类型与**环境变量名**，调用时网关从进程环境读取
- `filters`：路径/标签过滤，控制暴露面
- `max_tools`：工具数软上限，默认 100，超出拒绝注册并提示按 tag 拆分（工具过多会显著降低 LLM 选择准确率）
- `allowed_tools`：运行时工具白名单（M3）。同一 spec 可注册多个服务、各自暴露不同子集；list/call 两层都过滤，白名单外调用返回 -32601；空 = 全部暴露
- `response_template` / `tool_response_templates`：响应模板（见第 7 节）

## 6. 接口 → 工具映射规则

| OpenAPI 元素 | MCP Tool 字段 | 规则 |
|---|---|---|
| `operationId` | `name` | 清洗为 `字母/数字/_/-`、≤64；缺失时由 `method + path` 生成（`/pets/{id}` → `get_pets_id`）；冲突加序号 |
| `summary` + `description` | `description` | 拼接并附 method/path，让 LLM 准确理解用途 |
| `parameters`（path/query/header） | `inputSchema` 顶层属性 | 类型、required、enum、format 一一保留；path 参数强制必填 |
| `requestBody.schema` | `inputSchema.body` | 整体作为 `body` 对象，避免与参数名冲突 |
| `responses` | 结果约定 | 2xx JSON 原样返回（超限截断）；非 2xx 以 MCP `isError` 返回状态码 + 响应体摘要（默认 4KB） |
| `securitySchemes` | 认证识别 | 实际凭证来自注册配置，不从 spec 取 |

示例——spec 片段：

```json
"/pets/{petId}": {
  "get": {
    "operationId": "getPetById",
    "summary": "按 ID 查询宠物",
    "parameters": [
      { "name": "petId", "in": "path", "required": true, "schema": { "type": "integer" } }
    ]
  }
}
```

生成：

```json
{
  "name": "get_pet_by_id",
  "description": "按 ID 查询宠物（GET /pets/{petId}）",
  "inputSchema": {
    "type": "object",
    "properties": { "petId": { "type": "integer", "description": "路径参数 petId" } },
    "required": ["petId"]
  }
}
```

## 7. 稳定性与安全清单

**热更新语义**：新增服务 = 挂新路由，立即生效；更新服务 = 旧 session 全部作废（404，客户端自动重连），新 session 看到新工具集。不追求无缝热替换。

**隔离性**：
- per-service 独立 httpx client（独立超时/连接池）+ 熔断，一个慢上游拖不垮全局；
- per-service 并发限流，保护目标 API；
- 大 spec 解析丢线程池，不阻塞事件循环。

**安全**：
- 管理面 `Authorization: Bearer <admin-token>`；数据面 `X-Api-Key: <per-service-key>`；
- 目标主机 allowlist（`OPENAPI_MCP_HUB_HOST_ALLOWLIST`），防 SSRF（M2 加固）：
  - IP 字面量与网段比对，`::ffff:10.0.0.1` 等 IPv4-mapped IPv6 先规范化再比对（防绕过/防误杀）；
  - 未列名域名在注册时做 **DNS 解析核验**：所有解析 IP 必须命中网段条目（防 DNS rebinding）；
  - spec_url 拉取不自动跟随重定向：逐跳跟随且每跳重新过 allowlist；invoker 显式 `follow_redirects=False`；
  - 名字条目（精确/子域名）视为显式信任，不做解析；
- 凭证只引用环境变量名，不落盘、不进日志；
- spec 入参限制：体积上限、`$ref` 嵌套深度上限（防解析炸弹）；
- 信任边界认知：**tool description 会进入 LLM 上下文**，注册权限即影响所有接入 agent 的行为，admin token 是唯一闸门。

**持久化**：注册数据原子写盘（tmp + rename），网关重启后自动全量恢复，agent 侧 URL 永不变。

### 响应模板（M2）

默认工具输出是目标 API 的原始 JSON passthrough。注册时可配置模板把响应裁剪成 LLM 友好文本（省 token、可隐藏敏感字段）：

```json
{
  "name": "crm",
  "response_template": "共 {{response.total}} 条记录",
  "tool_response_templates": {
    "get_customer": "客户 {{response.data.name}}（{{response.data.level}}），余额 {{response.data.balance}}"
  }
}
```

- `response_template`：服务级默认，对所有工具生效；
- `tool_response_templates`：工具级覆盖，优先于服务级；
- 语法：`{{response}}` 整体输出（紧凑 JSON）；`{{response.a.b.0.c}}` 点路径 safeGet 取值（缺字段/越界渲染为空串，绝不报错）；`{{status}}`/`{{tool}}`/`{{ok}}`；
- 注册时校验语法（非法拒收 422），渲染异常自动回退 passthrough，绝不影响调用结果；

### 错误模型（M2）

工具调用失败时返回 `isError=true` + 结构化 JSON 载荷（对齐 JSON-RPC 错误对象）：

```json
{"error": {"code": -32000, "message": "目标 API 返回 HTTP 404: ...", "data": {"http_status": 404, "tool": "get_pet_by_id", "service": "petstore"}}}
```

| code | 含义 |
|---|---|
| -32601 | 未知工具（服务可能刚更新，请重新拉取工具列表） |
| -32602 | 入参未过 inputSchema 校验 / 缺少路径参数 |
| -32603 | 网关内部错误 |
| -32000 | 目标 API 返回非 2xx（`data.http_status` 为原始状态码） |
| -32001 | 目标 API 凭证缺失（环境变量未配置） |
| -32002 | 网络错误 / 超时 |

### 多版本 spec 支持（M2）

- **Swagger 2.0**：自动归一化为 OpenAPI 3.0 结构后走同一流水线（`host/basePath/schemes → servers`、`definitions → components.schemas` + `$ref` 前缀重写、`in: body` → requestBody、**formData 字段合并为 JSON body 属性**（warning 提示）、内联 `type` 包装为 schema、path 参数强制 required）；
- **OpenAPI 3.1**：支持，布尔 schema 归一化为空 schema，`type` 数组透传（外部 `$ref` 暂不支持，warning 提示）；
- 落盘始终保存**原始 spec**（含 Swagger 2.0 原稿），恢复时重走归一化流水线。

### 调用链细节（M3，对照 Unla 吸收）

- **表单/multipart 提交**：requestBody 为 `multipart/form-data` 或 `application/x-www-form-urlencoded` 时，字段同样提为工具参数，invoker 按表单方式提交；字段值传 `{filename, content, encoding: "base64"}` 即作为文件上传（文本字段直接传值）；
- **schema default 填充**：LLM 未传的可选参数自动填上 spec 声明的 `default`（避免下游默认值与 spec 声明不一致）；
- **模板 `||` 默认值**：`{{response.owner.name || 未登记}}`，取值非空时忽略默认值（`"0"` 视为非空）；
- **调用日志**：`GET /services/{name}/calls?limit=50`，内存环形缓冲 200 条（工具/状态/耗时 ms/错误码/消息摘要，重启清零），不落盘不含响应原文。

> 对照结论：路径参数转义（`quote(safe="")` 比 Unla `PathEscape` 更严）、cookie 参数、重定向 SSRF、解压、错误模型、注册期模板校验均已领先或持平；OTel/Prometheus、DB 版本管理、多租户、forward headers、per-tool 代理为刻意不采纳项。

## 8. 项目结构（规划）

```
openapi-mcp-hub/
├── README.md
├── pyproject.toml
├── docker-compose.yml          # 端口 20033 + data 卷
├── src/openapi_mcp_hub/
│   ├── main.py                 # FastAPI 入口、启动恢复
│   ├── admin.py                # 管理面 /services CRUD
│   ├── registry.py             # 注册表：读写锁 + 落盘 + 恢复
│   ├── mcp_host.py             # 每服务 FastMCP 实例，挂载 /mcp/{name}
│   ├── loader.py               # spec 加载/校验/$ref 解析（含循环检测）
│   ├── models.py               # 中间模型：ApiEndpoint / ApiParam …
│   ├── extractor.py            # 接口提取
│   ├── generator.py            # ApiEndpoint → MCP Tool
│   ├── invoker.py              # per-service httpx + 熔断 + 限流 + 错误映射
│   └── config.py               # 环境变量与网关级配置
├── examples/
│   ├── register-petstore.json  # 注册请求示例
│   └── petstore-openapi.json
└── tests/
    ├── fixtures/               # $ref、循环引用、deprecated、巨型 spec 样例
    ├── test_loader.py / test_extractor.py / test_generator.py
    └── test_e2e.py             # 注册 → MCP 客户端 → mock API 全链路
```

## 9. 技术选型与环境要求

- Python ≥ 3.10；`fastapi` + `uvicorn`、`mcp`（官方 SDK，FastMCP）、`httpx`、`pydantic`、`PyYAML`；测试 `pytest` + `respx`
- 网关环境变量：

| 变量 | 说明 |
|---|---|
| `OPENAPI_MCP_HUB_ADMIN_TOKEN` | 管理面 token（必填） |
| `OPENAPI_MCP_HUB_DATA_DIR` | 注册数据目录，默认 `./data` |
| `OPENAPI_MCP_HUB_HOST_ALLOWLIST` | 允许转发的目标主机/网段，逗号分隔；空 = 不限制 |
| 各服务凭证 | 由注册时的 `token_env` 引用，如 `GRANDE_TOKEN` |

- 建议给网关一个内网域名（如 `mcp.internal.example`）：裸 IP 迁移时所有 agent 配置都得改。

## 10. 开发计划（Roadmap）

| 里程碑 | 内容 | 交付物 |
|---|---|---|
| **M1** ✅ | 网关骨架：FastAPI + Registry（落盘/恢复）+ Loader/Extractor/Generator + 管理面 CRUD + `/mcp/{name}` 挂载 + 双层鉴权 | 已交付：注册 petstore spec 后，MCP 客户端能列出并调用工具（e2e + 冒烟全链路通过） |
| **M2** ✅ | 调用链加固：响应模板、SSRF 加固（DNS 核验/重定向逐跳/mapped IPv6）、结构化错误模型、Swagger 2.0 + OpenAPI 3.1、br/zstd 解压 | 已交付：121 项测试全通过（借鉴 Unla 设计，见第 7 节） |
| **M3** ✅ | 可观测与 DX：调用日志（GET /services/{name}/calls）、预览端点（POST /services/preview + sample_response 模板渲染）、运行时工具白名单 allowed_tools、表单/multipart 提交、schema default 填充、模板 `\|\|` 默认值 | 已交付：138 项测试全通过；对照 Unla 逐项审查后仅吸收上述 6 项，其余（多租户/Redis/OTel/Prometheus/版本管理）明确不做 |
| **M4** 🚧 | SSE 兼容端点（`/mcp/{name}/sse` + `/message`）、健康探测（GET /services/{name}/health）、Docker 部署（Dockerfile + compose） | 已交付前 3 项：双 transport 与 streamable 共享回调与鉴权、HEAD/GET 探测 + 凭证 env 自检、镜像构建与容器全链路验证；剩导出独立 server、HA |

## 11. 贡献与联系

- 分支策略：`master` 为主干，功能开发用 `feat/*`，合并前过单测；
