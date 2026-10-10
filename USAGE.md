# api-to-mcp 使用指南

> 面向两类人：**注册方**（有 REST API 想暴露给 agent 用）和**使用方**（想把工具接进 Claude Code 等 agent）。
> 网关部署地址以下用 `http://localhost:20033` 演示，替换成你的实际地址。

## 〇、三十秒理解

```
你有 openapi.json ──POST /services──▶ 网关 ──即刻生成──▶ http://<网关>/mcp/<服务名>
                                                          （Claude Code 填这个 URL 即可）
```

管理面用 `Authorization: Bearer <admin-token>`；数据面（agent 侧）用每服务独立的 `X-Api-Key`。

---

## 一、注册方：把 API 变成 MCP 服务

### 0. 偷懒方式：浏览器控制台

网关自带一个轻量控制台（单文件静态页，零构建零依赖）：

**浏览器打开 `http://localhost:20033/`**，顶栏粘贴管理 token（存在你浏览器 localStorage，
不上传服务端），即可：注册（REST/数据库两个 tab，表单+预检）、服务列表、详情
（工具清单/健康探测/最近调用日志）、key 轮换与删除。api_key / Claude Code 接入命令
均带一键复制。

命令行党继续往下看 curl；两种方式等价（背后同一套 admin API）。

### 1. 最简注册

```bash
ADMIN_TOKEN=$(cat deploy/admin_token)   # 网关管理员 token

curl -s -X POST http://localhost:20033/services \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "my-api",
    "title": "我的业务 API",
    "spec": <你的 openapi.json 内容>,
    "base_url": "http://my-api.internal:8080",
    "owner": "zhangjikang"
  }'
```

响应（**`api_key` 只此一次**，落盘只存哈希，丢了只能轮换重发）：

```json
{
  "name": "my-api",
  "mcp_url": "http://localhost:20033/mcp/my-api",
  "api_key": "psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "tool_count": 12,
  "tools": ["list_users", "get_user_by_id", "..."],
  "warnings": []
}
```

- `name`：`^[a-z][a-z0-9-]{1,31}$`，注册后不可改（使用方 URL 里写死了）
- `spec`（JSON 对象）/ `spec_text`（JSON 或 YAML 文本，上传文件常用）/ `spec_url`（网关可拉取的 URL）三选一
- `base_url`：目标 API 真实地址；不填则取 spec 的 `servers[0]`（常有坑，建议显式传）
- Swagger 2.0 / OpenAPI 3.0 / 3.1 都支持；multipart/表单接口自动按表单提交（文件字段传 `{filename, content, encoding: "base64"}`）

### 2. 目标 API 需要认证时

凭证**只写环境变量名，值由网关运维注入进程环境，永不落盘**：

```json
"auth": {"type": "bearer", "token_env": "MY_API_TOKEN"}
```

| 类型 | 字段 | 说明 |
|---|---|---|
| `bearer` | `token_env` | 注入 `Authorization: Bearer <env值>` |
| `api_key` | `key_name` + `location`（header/query）+ `value_env` | 注入自定义 key 头或 query 参数 |
| `basic` | `user_env` + `password_env` | HTTP Basic |

环境变量没配时调用返回 `-32001`，用 `GET /services/{name}/health` 的 `auth_env_ok` 一眼确认。

### 3. 控制暴露面（强烈建议做）

工具太多会显著降低 LLM 选择准确率，三层裁剪按需用：

```json
"filters": {"exclude": ["/internal/*", "/admin/**"]},
"max_tools": 50,
"allowed_tools": ["list_pets", "get_pet_by_id"]
```

- `filters.exclude/include`：注册期路径裁剪（fnmatch 通配）
- `allowed_tools`：**运行时白名单**——同一份 spec 可注册多个服务各暴露不同子集（如 `my-api-readonly` 只放查询工具）；白名单外的调用返回 `-32601`

### 4. 让返回值更适合 LLM 阅读：响应模板

默认把下游 JSON 原样返回。配置模板可裁剪成一句话（token 更省、重点更清楚）：

```json
"response_template": "宠物 {{response.name}}（id={{response.id}}，{{response.status}}）",
"tool_response_templates": {"get_pet_by_id": "专用模板，覆盖服务级默认"}
```

语法：`{{response.a.b.0.c}}` 点路径安全取值；`{{response.owner || 未登记}}` 缺失时用默认值；`{{status}}` HTTP 状态码。渲染失败自动回退原始 JSON，绝不影响调用。

**注册前先预览模板效果**（不落盘、不生成端点）：

```bash
curl -s -X POST http://localhost:20033/services/preview \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{...注册体..., "sample_response": {"name": "小白", "id": 1}}'
# 响应里 template_preview 直接给出每个工具的渲染结果
```

### 5. 日常运维

### 6. 不写接口直接查库：数据库注册（M5）

没有 REST API 的 PostgreSQL 库，可以直接注册成 MCP 服务（只读）：

```bash
# ① DSN 放进网关环境（密码永不落盘/入日志）
export WORKFLOW_DB_URL='postgresql://ro_user:密码@db.internal.example:5432/workflow'   # @ 需 URL 转义 %40
#   MySQL 8+ 同理：mysql://ro_user:密码@host:3306/dbname（方言由前缀自动推断，工具形态一致）

# ② 注册（db 与 spec 二选一；重启网关前 export 才能被读到）
curl -X POST http://localhost:20033/services -H "Authorization: Bearer $ADMIN_TOKEN" -d '{
  "name": "workflow-db",
  "db": {"url_env": "WORKFLOW_DB_URL",
         "exclude_tables": ["*_audit", "tmp_*"],   # 可选：fnmatch 通配过滤
         "max_rows": 200,                            # 可选：单次返回行数上限
         "timeout_s": 15}
}'
```

注册后生成 **3 个工具**（工具数恒定，表多不爆炸）：

| 工具 | 用途 |
|---|---|
| `list_tables` | 列表/视图（含列清单、主键、注释、行数估计） |
| `describe_table` | 单表完整结构 |
| `query` | 只读执行单条 SELECT/WITH/EXPLAIN；schema 摘要已写进工具描述，LLM 可盲写 SQL |

**安全边界**（写操作一律拒，LLM 拿到的只有只读查询权）：
- 语句白名单：首 token 仅 SELECT/WITH/EXPLAIN，禁多语句、禁 SQL 注释 → 违规报 `-32602`（错误信息会告诉 LLM 为什么被拒）；
- 每条查询包在 `READ ONLY` 事务里执行（纵深：运维侧建议再配只读账号）；
- 行数/超时上限：结果外层强制 LIMIT，超时即断；
- 排除的表不出现在任何工具的输入/输出里。

运维：`GET /services/{name}/health` 返回 `kind: "db"`（DSN 脱敏展示）；
调用日志可看到每条 SQL 的成败与耗时。**建议给生产库配只读账号**（`CREATE ROLE ro ...`）。

> 表结构变更（加列/新表）后需 `PUT /services/{name}` 重注册刷新；限制收紧、排除表调整同理。
> MySQL 特有：`schema` 字段即库名（缺省取 DSN 库名）；超时走 `max_execution_time`（仅作用 SELECT），需 MySQL 8.0+；连接强制 utf8mb4。

| 想知道/想做 | 命令 |
|---|---|
| 服务还活着吗 | `GET /services/{name}/health` → reachable / http_status / latency_ms / **auth_env_ok** |
| agent 调用情况 | `GET /services/{name}/calls?limit=50` → 近期 200 条：工具/状态/耗时/错误码（重启清零） |
| 换 key（泄露/交接） | `POST /services/{name}/rotate-key` → 新 key 立即生效，旧 key 全部 401 |
| 更新 spec/配置 | `PUT /services/{name}`（整体替换；进行中的 MCP 会话作废，客户端自动重连） |
| 下线 | `DELETE /services/{name}`（落盘同步删除） |
| 工具完整定义 | `GET /services/{name}/tools` |

> 网关重启**自动恢复**全部已注册服务（文件后端 `data/services/*.json`；或 PostgreSQL，运维配 `API_TO_MCP_DATABASE_URL` 后生效），agent 侧 URL 与 key 不变。

---

## 二、使用方：把服务接进 agent

注册方会给你两样东西：**MCP URL** 和 **`psk-` 开头的 X-Api-Key**。

### Claude Code（推荐，Streamable HTTP）

```bash
claude mcp add --transport http petstore http://localhost:20033/mcp/petstore \
  --header 'X-Api-Key: psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

之后 `/mcp` 里能看到工具列表，模型即可调用 `list_pets`、`get_pet_by_id` 等。

### 只支持 SSE 的老客户端

改用兼容端点（同一套工具、同一个 key）：

```
SSE 地址:   http://localhost:20033/mcp/<服务名>/sse
消息端点:   自动协商（endpoint 事件下发），无需手填
```

### Python（脚本/自研 agent）

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx

http_client = httpx.AsyncClient(headers={"X-Api-Key": "psk-xxx"})
async with streamable_http_client(url, http_client=http_client, terminate_on_close=False) as (r, w):
    async with ClientSession(r, w) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("get_pet_by_id", {"petId": 1})
```

---

## 三、报错速查表

**HTTP 层**（连 MCP 端点都进不去）：

| 现象 | 原因 | 找谁 |
|---|---|---|
| `401` | X-Api-Key 缺失/错误/已被轮换 | 使用方找注册方要新 key |
| `404` | 服务名写错或已下线 | 核对 URL 里的服务名 |
| `406` | Accept 头不含 `application/json, text/event-stream` | 客户端库配置问题 |
| `503` | 网关正在启动/服务正在更新 | 稍后重试 |

**MCP 层**（`isError` 结果里的 JSON）：

| code | 含义 | 处理 |
|---|---|---|
| `-32601` | 未知工具，或**该工具不在 allowed_tools 白名单** | 检查工具名；白名单问题找注册方 |
| `-32602` | 入参校验失败 / 路径参数缺失 | 看 message，通常是参数类型错或漏了 required 参数 |
| `-32000` | 目标 API 返回非 2xx（`data.http_status` 是原始状态码） | 传参业务错误，或目标 API 有问题（用 health 探测确认） |
| `-32001` | 网关缺目标 API 凭证环境变量 | 网关运维注入 env 后即恢复 |
| `-32002` | 网络/超时（目标 API 不可达） | 用 `GET /services/{name}/health` 定位 |
| `-32603` | 网关内部错误 | 看网关日志 `logs/gateway.log` |

---

## 四、演示服务（可直接练手）

网关上跑着 petstore 演示（mock 目标 `127.0.0.1:20034`，Bearer `demo-secret-token`）：

```bash
claude mcp add --transport http petstore http://localhost:20033/mcp/petstore \
  --header 'X-Api-Key: psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

试：`get_pet_by_id(petId=1)` → `宠物 小白（id=1，dog）`；`petId=999999` → 404 结构化错误；`whoami` → 回显网关注入的凭证头。

---

## 附：完整注册字段参考

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✓ | 服务名，URL 的一部分，注册后不可改 |
| `spec` / `spec_text` / `spec_url` | 三选一 | OpenAPI 3.0/3.1 或 Swagger 2.0；spec_text 收 JSON/YAML 文本（控制台上传的文件即走此字段） |
| `base_url` | 建议 | 目标 API 地址；缺省取 spec servers[0] |
| `title` | | 展示名 |
| `auth` | | 见第一节 2 |
| `filters` | | `include` / `exclude` 路径通配、`exclude_tags` |
| `max_tools` | | 默认 100，超出拒绝注册 |
| `allowed_tools` | | 运行时白名单，空=全部 |
| `response_template` / `tool_response_templates` | | 响应模板（服务级默认 + 工具级覆盖） |
| `timeout_s` | | 目标 API 超时，默认 30s |
| `owner` | | 负责人，仅记录 |
