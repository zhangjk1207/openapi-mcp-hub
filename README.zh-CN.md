# openapi-mcp-hub

OpenAPI → MCP 网关。起一个服务（比如 `localhost:20033`），POST 一份 `openapi.json` 过去，马上得到一个 MCP 端点：`http://localhost:20033/mcp/{service-name}`，agent 可以直接用。

没有 REST API、只有数据库的，可以直接注册一个 PostgreSQL 库：网关内省库表，生成只读查询工具，管理和 REST 服务同一套。

一个端口，路径级多租户，注册即生效。不用为每份 spec 部署一个进程、占一个端口。

网关根路径带一个控制台，单文件静态页，没有构建链和外部资源。能表单注册（带预检）、看服务列表和详情、健康探测、调用日志、轮换 key。内网环境直接用。

> 当前版本 v0.6.1，279 项测试通过。怎么用看 [USAGE.md](USAGE.md)，英文文档 [README.md](README.md)。

![demo](docs/demo.gif)

---

## 1. 为什么要做这个

- OpenAPI 是 REST 接口描述的事实标准，内部大部分服务都有现成的 `openapi.json`；
- MCP 已经是 agent 接工具的主流协议，Claude Code 这类客户端原生支持 Streamable HTTP；
- 手工给每个接口包一层 MCP tool，重复而且难维护；反过来，每份 spec 部署一个 MCP 服务，端口和运维成本都会随服务数线性涨。

所以做成网关：一个常驻进程，注册多少份 spec 就动态挂多少个 MCP 端点，都在同一个端口下。

另外两件事值得提前说。2026 年几份公开的 MCP 生态安全报告说的都是同一件事：大约只有 8.5% 的 MCP server 在用 OAuth；上游悄悄改工具描述（tool poisoning / rug pull）这种事，一般要等 agent 行为异常才被发现。这两件事这个网关都直接处理了——数据面认证见 5.1，工具变更可见性见第 7 节。

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
│ Invoker：per-service httpx client + 超时 + 连接池隔离 + 401 重试    │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
                        各目标 REST API
```

- 管理面 `/services/*`，面向注册方（人或 CI），管服务生命周期；
- 数据面 `/mcp/{name}`，面向 agent，Streamable HTTP 端点，Claude Code 填 URL 就能接；
- SSE 兼容通道 `/mcp/{name}/sse`（GET）+ `/mcp/{name}/message?session_id=...`（POST），给老客户端，与 streamable 共享同一套工具回调与鉴权，两个通道可以同时用；
- 两个命名空间物理隔离，服务名不可能和管理路由撞上（对比 `/{name}/mcp` 方案：注册一个叫 `add` 的服务就撞了）。

## 3. 三十秒上手

```bash
# ① 启动网关（一次）
export OPENAPI_MCP_HUB_ADMIN_TOKEN=<管理token>
python -m openapi_mcp_hub          # 监听 0.0.0.0:20033（OPENAPI_MCP_HUB_HOST/PORT 可改）

# ② 注册一个服务：spec + 真实地址 + 凭证引用（注意：光有 openapi.json 不够）
curl -X POST http://localhost:20033/services \
  -H "Authorization: Bearer $OPENAPI_MCP_HUB_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d @examples/register-petstore.json

# ③ 响应直接给出接入命令（注册即生效，不用重启）
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

之后 LLM 调工具，网关把参数还原成真实 HTTP 请求发给目标 API，结果再结构化地回去。

本地开发与测试：

```bash
pip install -e ".[dev]"     # pytest/pytest-asyncio/respx 等一起装上
python -m pytest tests/      # 279 个用例：单测 + respx mock + e2e（真实 uvicorn 双服务 + MCP 客户端）
```

适配说明：基于 mcp SDK 2.x（2.3+）。lowlevel `Server` 构造器注入 `on_list_tools`/`on_call_tool` 回调，types 是 snake_case，入参校验 SDK 2.x 不再内置，网关用 jsonschema 按 inputSchema 自己做。运行环境建议项目独立 venv（`python -m venv .venv && .venv/bin/pip install -e ".[dev]"`），不碰机器上的共享环境。

### 60 秒体验（Docker）

```bash
git clone https://github.com/zhangjk1207/openapi-mcp-hub && cd openapi-mcp-hub
docker compose up -d       # 网关 + PostgreSQL + 演示 API，自动注册 petstore 演示服务
```

打开 http://localhost:20033，演示管理 token：`demo-admin-token`。里面已经有一个能直接用的 `petstore` 服务：

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

演示模式（compose 里的 `OPENAPI_MCP_HUB_DEMO=1`）用固定公开的演示 key，只是为了省事。正式使用前关掉演示模式、改掉 `OPENAPI_MCP_HUB_ADMIN_TOKEN`——真实注册返回的永远是一次性随机 key。

### 安装

```bash
pip install openapi-mcp-hub        # 或：uvx openapi-mcp-hub
openapi-mcp-hub --version

# 容器（多架构 amd64 / arm64）
docker run -d --name openapi-mcp-hub -p 20033:20033 \
  -e OPENAPI_MCP_HUB_ADMIN_TOKEN=换成你的token \
  -v "$PWD/data:/app/data" \
  ghcr.io/zhangjk1207/openapi-mcp-hub:latest
```

命令行参数**全部可选**，不传任何参数时行为与以前完全一致（纯读 `OPENAPI_MCP_HUB_*` 环境变量）；传入则覆盖环境变量。

| 参数 | 对应环境变量 | 默认值 |
|---|---|---|
| `--host` | `OPENAPI_MCP_HUB_HOST` | `0.0.0.0` |
| `--port` | `OPENAPI_MCP_HUB_PORT` | `20033` |
| `--data-dir` | `OPENAPI_MCP_HUB_DATA_DIR` | `./data` |
| `--database-url` | `OPENAPI_MCP_HUB_DATABASE_URL` | 文件后端 |
| `--host-allowlist` | `OPENAPI_MCP_HUB_HOST_ALLOWLIST` | 不限制 |
| `--docs` | `OPENAPI_MCP_HUB_DOCS=1` | 关闭 |
| `--log-level` | — | `info` |
| `--print-config` | — | 打印生效配置（凭证脱敏）后退出 |

“为什么起不来”绝大多数是配置问题，所以有了这个：

```bash
$ OPENAPI_MCP_HUB_ADMIN_TOKEN=x openapi-mcp-hub --print-config --port 8080
admin_token      : 已配置
listen           : 0.0.0.0:8080
data_dir         : /app/data
store            : file
host_allowlist   : 不限制（内网默认）
docs             : off
max_tools/service: 100
```

管理面的 `/docs` 与 `/openapi.json` **默认关闭**（控制台已覆盖全部操作，没必要对外暴露 schema）；需要时加 `--docs` 或 `OPENAPI_MCP_HUB_DOCS=1`。

### 接入各类 agent 客户端

端点固定：`http://<host>:20033/mcp/<服务名>`，鉴权用该服务的 `X-Api-Key`（jwt 模式下改用 `Authorization: Bearer <token>`）。下面是各客户端的落点（字段名以客户端自身文档为准）：

| 客户端 | 落点 |
|---|---|
| **Claude Code** | `claude mcp add --transport http petstore <url> --header 'X-Api-Key: psk-…'` |
| **Claude Desktop** | `claude_desktop_config.json` → `mcpServers`（旧版本需 `mcp-remote` 桥） |
| **Cursor** | `~/.cursor/mcp.json` → `mcpServers` |
| **Cline / Roo** | MCP 设置 → `mcpServers`（远程服务器：URL + headers） |
| **VS Code（Copilot agent）** | `.vscode/mcp.json` → **`servers`** 键，带 `"type": "http"` |
| **Dify** | 工具 → MCP 服务器：填端点 URL + `X-Api-Key` 头 |
| **Open WebUI** | 设置 → 工具 → 添加 MCP 服务器（URL + header） |
| **其他** | 任意 MCP SDK 的 `streamable_http` 客户端，或下面的 SSE 兼容端点 |

通用的 JSON 形式（`mcpServers` 系的客户端直接可用）：

```json
{
  "mcpServers": {
    "petstore": {
      "url": "http://localhost:20033/mcp/petstore",
      "headers": { "X-Api-Key": "psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" }
    }
  }
}
```

只支持 SSE 的老客户端（工具与 key 完全相同）：

```
GET http://localhost:20033/mcp/<服务名>/sse
```

### 只能读 stdio 的客户端——用内置桥

有些客户端只会 spawn 本地进程、用 stdin/stdout 说 JSON-RPC。同一个包里带了一个 `bridge`
子命令（`uvx openapi-mcp-hub bridge …`，或本地安装后的 `openapi-mcp-hub bridge …`），
把 stdio 原样接到运行中的网关端点上：

```json
{
  "mcpServers": {
    "petstore": {
      "command": "uvx",
      "args": [
        "openapi-mcp-hub", "bridge",
        "--url", "http://localhost:20033/mcp/petstore",
        "--header", "X-Api-Key=psk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
      ]
    }
  }
}
```

桥只做协议搬运：消息原样转发，保持 `Mcp-Session-Id` 粘滞，从客户端自己的请求推导
`MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` 头，并把传输层失败（401、网关不可达）
翻译成带 `data.hint` 的 JSON-RPC 错误。**不缓存、不改写、不重复实现任何语义**，
客户端能直接说 Streamable HTTP 时完全不需要它。

### 常驻部署（无 Docker）

```bash
# ① 启动网关（0.0.0.0:20033；首次自动生成管理 token 存到 deploy/admin_token）
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
cp deploy/pg.env.example deploy/pg.env   # 填 DSN（密码特殊字符要 URL 转义）
DEMO_PETSTORE_TOKEN=xxx nohup deploy/run.sh ...   # 重启即生效
```

- 表结构：`services(name PK, title, base_url, api_key_hash, owner, created_at, updated_at, record JSONB)`，record 和文件模式同构；
- 首次切换自动迁移：pg 表为空且 data/services 有记录就导入，原文件保留。注意迁移是单向的：pg 期间新注册的服务不回写文件，去掉 pg.env 重启会回到迁移时点；
- pg 连不上时网关启动直接失败退出（fail fast，交给 supervisor/容器重启重试）；
- 集成测试：`OMH_PG_TEST_DSN=... pytest tests/test_store_pg.py`，没配置就自动跳过。

### 数据库 → MCP：不用写接口，直接查库

没有 REST API 的数据，直接把库注册进来（PostgreSQL / MySQL 8+，只读，方言由 DSN 前缀推断）。`db` 和 `spec` 二选一：

```bash
# ① DSN 含密码只经环境变量引用（永不落盘，和目标 API 凭证同一原则）
export WORKFLOW_DB_URL='postgresql://ro_user:***@db.internal.example:5432/workflow'   # MySQL：mysql://user:***@host:3306/db

# ② 注册：内省库表 → 生成三件套工具（工具数固定，表多也不会爆）
curl -X POST http://localhost:20033/services -H "Authorization: Bearer $ADMIN_TOKEN" -d '{
  "name": "workflow-db",
  "db": {"url_env": "WORKFLOW_DB_URL", "schema": "public",
         "exclude_tables": ["*_audit"], "max_rows": 200}
}'
# → 工具：list_tables / describe_table / query（schema 摘要写进 query 描述，LLM 可以直接盲写 SQL）
```

只读是六层强制的（详见 docs/PLAN-DB-MCP.md）：DSN 环境变量引用、只读三重强制（READ ONLY 事务 + 语句白名单 + 建议只读账号）、语句超时（pg 用 statement_timeout，mysql 用 max_execution_time）、行数上限（外层 LIMIT 包裹）、独立连接池、表过滤，外加 SQL 摘要进调用日志。写操作一律拦截（-32602）。

和 REST 服务同窗管理：同一个 `/mcp/{name}`，同一套 key、鉴权、持久化、健康探测、观测。

### Docker 部署

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

镜像里验证过的：容器内注册、数据卷重启恢复、health 凭证自检（缺 env 时 `auth_env_ok:false`）、streamable 与 SSE 双通道全链路。

### 控制台

注册（文件上传 / 拖拽）、查看生成工具与调用日志、轮换 key，全在一个静态页里：无构建链、无外部资源，断网内网也能用。

| 服务列表 | 服务详情 |
|---|---|
| ![服务列表](docs/screenshots/console-list.png) | ![服务详情](docs/screenshots/console-detail.png) |

| 注册 REST API | 注册数据库（只读） |
|---|---|
| ![注册 REST API](docs/screenshots/console-register-rest.png) | ![注册数据库](docs/screenshots/console-register-db.png) |

### 面向 agent 的工具质量

tools/list 会整个进入模型上下文，所以“工具怎么描述、怎么标注”直接决定 agent 能不能用对。这一块做了三件事，全部零配置：

**① 安全标注（MCP ToolAnnotations）**：根据 HTTP 方法与动词自动推断，控制台工具列表里直接显示徽标。

| 方法/动作 | 只读 | 破坏性 | 幂等 |
|---|---|---|---|
| `GET` / `HEAD` | ✅ | — | ✅ |
| `DELETE` / `PUT` / `PATCH` | — | ✅ | DELETE/PUT 幂等 |
| `POST` + `create_*` 类 | — | — | — |
| `POST` 语义不明或含 delete/drop/reset… | — | ✅ | — |
| 数据库三件套 | ✅（执行层 READ ONLY 事务兜底） | — | ✅ |

原则是**保守优先**：MCP 规范里 `destructiveHint` 默认就是 true，所以只在有把握时才降级；拿不准的 POST 一律按破坏性处理。这样 agent 可以直接跑只读工具，遇到改数据的会先问一句。

**② 描述与 schema 体积压缩**：剥 HTML/Markdown 装饰、折叠空白、丢 `title`/`$comment` 这类纯展示键、超长文本按句子边界截断（工具描述 ≤320 字符、字段说明 ≤160），并显式标上省略号。

实测（40 个接口 × 3 方法的合成 spec）：

| 文档风格 | 压缩前 | 压缩后 | 节省 |
|---|---|---|---|
| 典型企业文档 | 52,150 字（434/工具） | 46,560 字（388/工具） | 10.7% |
| Javadoc 式超长文档 | 284,250 字（2,368/工具） | 71,320 字（594/工具） | **74.9%** |

只做不改变语义的裁剪：`required` / `enum` / `format` / `default` 一律原样保留，不会因此让 agent 传错参。

**③ 可自纠正的错误信息**：错误载荷统一带 `data.hint`，告诉 agent 下一步该改什么、以及重试到底有没有用。

| 场景 | hint 语义 |
|---|---|
| 入参校验失败 | 列出本工具合法参数名与必填项，并明确“不要自造字段名” |
| `401` / `403` / 凭证缺失 | 服务端配置问题，**重试不会成功** |
| `404` / `405` | 参数或工具选错，不要原样重发 |
| `409` | 先查当前状态再决定 |
| `422` | 按错误信息里的字段名修正 |
| `429` / `5xx` / 超时 | 退避后重试；连续失败说明上游异常 |

**④ 渐进式工具发现（progressive tool discovery）**：工具数 ≥8 时自动挂一个 `search_tools`
元工具；超过 `--advertise-limit`（默认 50）时 `tools/list` 只列最常用的那批，其余靠检索发现。

检索是**纯本地 BM25 词法索引**：索引只来自工具自身的文字（名称、路径、描述、参数名），
不引 embedding、不调模型、不加新依赖。中文用字符 bigram 切分，所以「删除宠物」「按 petId 查」
这种说法都能命中。

为什么默认 50：Anthropic 官方文档称可用工具超过 30–50 个后选择准确率开始下降，而五个 server
的工具定义就有约 55k token。注册一个真实 API（Stripe 量级）时，把所有工具一次性塞给模型
是网关自己造成的不可用。

`tools/list` 同时声明 `ttlMs`（5 分钟）与 `cacheScope: public`，并固定「只读优先 + 名字升序」的
顺序——规范明确要求确定性顺序以提升 prompt cache 命中率。这两项只对 2026-07-28 及以上的客户端生效，
旧版客户端会忽略。

## 4. 关键决策（已定）

| 决策点 | 结论 | 理由 |
|---|---|---|
| 管理面鉴权 | 静态 admin token | `OPENAPI_MCP_HUB_ADMIN_TOKEN` 环境变量注入，内网够用 |
| 目标 API 凭证 | 网关代持 | 注册时只引用环境变量名（如 `GRANDE_TOKEN`），凭证永不落盘 |
| 数据面鉴权 | 每服务独立 key（默认）或 JWT | key：注册时生成、只返回一次、落盘哈希、可轮换，请求头 `X-Api-Key`；JWT 见 5.1 |
| 首批客户端 | Claude Code | 原生支持 Streamable HTTP；Cursor / Cline / LangGraph 也能用；Claude Desktop 要走 mcp-remote 桥 |

## 5. 管理面 API

| 方法 | 路径 | 作用 | 说明 |
|---|---|---|---|
| POST | `/services` | 注册 | 解析 → 生成工具 → 挂载 → 落盘，同步返回；同名返回 409，覆盖必须显式 PUT |
| GET | `/services` | 列表 | 健康状态、工具数、owner、更新时间 |
| POST | `/services/preview` | 预览 | 完整走流水线但不落盘、不生成端点和 key；可选 `sample_response` 直接渲染模板效果 |
| GET | `/services/{name}` | 详情 | 配置 + 工具清单 + 目标 API 健康探测结果 |
| GET | `/services/{name}/health` | 健康探测 | 对目标 API 发 HEAD（405 时回退 GET），返回 reachable/状态码/延迟 ms；顺带自检凭证环境变量配没配（不回传值） |
| GET | `/services/{name}/calls` | 调用日志 | 近 200 条工具调用（内存环形缓冲，重启清零）：工具/状态/耗时/错误码 |
| GET | `/services/{name}/stats` | 调用统计 | 环形缓冲聚合：成功率 / p50 / p95 / 热点工具 |
| PUT | `/services/{name}` | 更新 | 替换 spec / base_url / 过滤规则；更新即作废该服务所有 MCP session（客户端会自动重连重新 initialize） |
| POST | `/services/{name}/refresh` | 重拉 spec | 仅 spec_url 注册源：重新拉 spec 热更新，api_key 与 created_at 保留，上游改接口不用重发注册体 |
| POST | `/services/{name}/token` | 签发短期 token | 仅内置 jwt 模式（见 5.1），TTL 60s–24h |
| DELETE | `/services/{name}` | 下线 | 卸载路由 + 删落盘；agent 侧表现为连接失败 |
| POST | `/services/{name}/rotate-key` | 轮换 | 重新生成数据面 key（static 模式） |

注册请求体：

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

- `name`：必填，`^[a-z][a-z0-9-]{1,31}$`，注册后不可改（agent 配置里写死了 URL）；`title` 只做展示，内部 spec 标题多为中文，不做自动命名；
- `spec` / `spec_url`：二选一；JSON/YAML 都行，OpenAPI 3.0/3.1；
- `base_url`：目标 API 真实地址；缺省取 spec 的 `servers[0]` 并给 warning（spec 里的 servers 经常不可信）；
- `auth`：`bearer` / `api_key`（header 或 query）/ `basic` / `oauth2`（client_credentials：网关代取并缓存 token、提前 60s 刷新、401 自动重试一次）；只存类型与环境变量名（oauth2 的 `token_url` 是注册期静态配置，同样过 allowlist），调用时网关从进程环境读；
- `authn`：数据面认证，见 5.1；
- `filters`：路径/标签过滤，控制暴露面；
- `max_tools`：工具数上限，默认 100，超出拒绝注册并提示按 tag 拆分（工具太多会明显降低 LLM 选对的概率）；
- `allowed_tools`：运行时工具白名单。同一 spec 可以注册多个服务、各暴露不同子集；list 和 call 两层都过滤，白名单外调用返回 -32601；空 = 全部暴露；
- `response_template` / `tool_response_templates`：响应模板，见第 7 节。

### 5.1 数据面认证（v0.4.0）：X-Api-Key 或 JWT

注册时 `authn` 字段控制数据面（agent → `/mcp/{name}`）怎么认证：

| 模式 | 配置 | 行为 |
|---|---|---|
| static（默认） | `authn` 缺省 | `X-Api-Key`：注册返回一次、落盘哈希、可轮换 |
| jwt，内置签发 | `{"mode":"jwt"}` | 网关用 `OPENAPI_MCP_HUB_JWT_SECRET`（HS256，密钥只在进程环境）签发短期 token：`POST /services/{name}/token`，`ttl_s` 上限 24h，aud 绑定服务名，到期自动失效 |
| jwt，外部 IdP | `{"mode":"jwt","jwks_url":"…","issuer":"…","audience":"…"}` | 网关只做 resource server：JWKS 验签（算法白名单 RS256/ES256）、iss/aud 校验；`/.well-known/oauth-protected-resource/mcp/{name}` 提供 RFC 9728 发现，Claude Code 这类按 spec 走的客户端可以对接着企业 Keycloak/Auth0 走完整 OAuth 2.1 流程 |

jwt 模式下该服务的 `X-Api-Key` 通道关闭；401 响应带 `WWW-Authenticate` 头，指向资源元数据。密钥和私钥不落盘：外部模式只存 JWKS URL（拉的是公钥），内置模式密钥只在环境变量里。

## 6. 接口 → 工具映射规则

| OpenAPI 元素 | MCP Tool 字段 | 规则 |
|---|---|---|
| `operationId` | `name` | 清洗为字母/数字/`_`/`-`、≤64；缺失时由 `method + path` 生成（`/pets/{id}` → `get_pets_id`）；冲突加序号 |
| `summary` + `description` | `description` | 拼接并附 method/path，让 LLM 知道这个工具是干嘛的 |
| `parameters`（path/query/header） | `inputSchema` 顶层属性 | 类型、required、enum、format 一一保留；path 参数强制必填 |
| `requestBody.schema` | `inputSchema.body` | 整体作为 `body` 对象，避免和参数名冲突 |
| `responses` | 结果约定 | 2xx JSON 原样返回（超限截断）；非 2xx 以 MCP `isError` 返回状态码 + 响应体摘要（默认 4KB） |
| `securitySchemes` | 认证识别 | 实际凭证来自注册配置，不从 spec 取 |

spec 片段：

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

## 7. 稳定性与安全

热更新：新增服务挂新路由，立即生效。更新服务会把该服务的旧 session 全部作废（404，客户端会自动重连），新 session 拿到新工具集。不做无缝热替换。

隔离：

- 每个服务独立 httpx client（独立超时 / 独立连接池，`minsize=1, maxsize=5`），一个慢上游拖不垮全局；
- 熔断与 per-service 并发限流**尚未实现**（见 §10 Roadmap），当前靠独立超时 + 连接池上限把故障关在单服务内；
- 大 spec 的解析丢线程池，不阻塞事件循环。

安全：

- 管理面 `Authorization: Bearer <admin-token>`；数据面 `X-Api-Key` 或 JWT（5.1）；
- 目标主机 allowlist（`OPENAPI_MCP_HUB_HOST_ALLOWLIST`），防 SSRF：
  - IP 字面量与网段比对，`::ffff:10.0.0.1` 这类 IPv4-mapped IPv6 先规范化再比（防绕过也防误杀）；
  - 没列名的域名在注册时做 DNS 解析核验：所有解析 IP 必须命中网段条目（防 DNS rebinding）；
  - spec_url 拉取不自动跟重定向：逐跳跟随、每跳重新过 allowlist；invoker 显式 `follow_redirects=False`；
  - 名字条目（精确/子域名）视为显式信任，不做解析；
- 凭证只引用环境变量名，不落盘、不进日志；
- spec 入参有限制：体积上限、`$ref` 嵌套深度上限（防解析炸弹）；
- 数据面 JWT（v0.4.0）：外部 IdP 模式（JWKS 验签、iss/aud 校验、算法白名单、RFC 9728 发现）与内置签发模式（HS256 短期 token、aud 绑定服务、密钥只在环境变量）。key 和 token 按服务独立选。
- 工具变更可见性（v0.4.0，防 tool poisoning）：每次 `PUT /services/{name}` 与 `POST /services/{name}/refresh` 返回 `tool_changes`——新增、下线、描述变化、入参变化四类 diff；描述变了会附带人工复核提示。理由很直接：描述就是 LLM 看到的指令，上游静默改描述（rug pull）是 2026 年公开报告里 MCP 生态的高频攻击面。

有一个边界要认清：tool description 会进入 LLM 的上下文。谁能注册服务，谁就能影响所有接入的 agent——所以 admin token 是唯一的闸门。

持久化：注册数据原子写盘（tmp + rename），网关重启自动全量恢复，agent 侧 URL 永不变。

### 响应模板

默认工具输出是目标 API 的原始 JSON。注册时可配模板，把响应裁成 LLM 好读的文本（省 token，也能藏敏感字段）：

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
- 语法：`{{response}}` 整体输出（紧凑 JSON）；`{{response.a.b.0.c}}` 点路径 safeGet 取值（缺字段/越界渲染成空串，不报错）；`{{status}}`/`{{tool}}`/`{{ok}}`；
- 注册时校验语法（非法拒收 422）；渲染出异常自动回退 passthrough，不影响调用结果。

### 错误模型

工具调用失败返回 `isError=true` + 结构化 JSON 载荷（对齐 JSON-RPC 错误对象）：

```json
{"error": {"code": -32000, "message": "目标 API 返回 HTTP 404: ...", "data": {"http_status": 404, "tool": "get_pet_by_id", "service": "petstore"}}}
```

| code | 含义 |
|---|---|
| -32601 | 未知工具（服务可能刚更新，重新拉一下工具列表） |
| -32602 | 入参没过 inputSchema 校验 / 缺路径参数 |
| -32603 | 网关内部错误 |
| -32000 | 目标 API 返回非 2xx（`data.http_status` 是原始状态码） |
| -32001 | 目标 API 凭证缺失（环境变量没配） |
| -32002 | 网络错误 / 超时 |

### 多版本 spec

- Swagger 2.0：自动归一化成 OpenAPI 3.0 再走流水线（`host/basePath/schemes → servers`、`definitions → components.schemas` + `$ref` 前缀重写、`in: body` → requestBody、formData 字段合并为 JSON body 属性并给 warning、内联 `type` 包装成 schema、path 参数强制 required）；
- OpenAPI 3.1：支持，布尔 schema 归一化为空 schema，`type` 数组透传（外部 `$ref` 暂不支持，给 warning）；
- 落盘始终存原始 spec（含 Swagger 2.0 原稿），恢复时重走归一化。

### 调用链细节

- 表单/multipart 提交：requestBody 是 `multipart/form-data` 或 `application/x-www-form-urlencoded` 时，字段同样提为工具参数，invoker 按表单方式提交；字段值传 `{filename, content, encoding: "base64"}` 就作为文件上传（文本字段直接传值）；
- schema default 填充：LLM 没传的可选参数自动填 spec 声明的 `default`（免得下游默认值和 spec 声明不一致）；
- 模板 `||` 默认值：`{{response.owner.name || 未登记}}`，取值非空时忽略默认值（`"0"` 算非空）；
- 调用日志：`GET /services/{name}/calls?limit=50`，内存环形缓冲 200 条（工具/状态/耗时 ms/错误码/消息摘要），重启清零，不落盘、不含响应原文。

### 与 Unla 的对照、与商业网关的关系

> 对照结论：路径参数转义（`quote(safe="")` 比 Unla 的 `PathEscape` 更严）、cookie 参数、重定向 SSRF、解压、错误模型、注册期模板校验均已领先或持平；OTel/Prometheus、DB 版本管理、多租户、forward headers、per-tool 代理是刻意不做的。

商业网关方面：Kong AI Gateway 也有 MCP 治理，但 RBAC/OIDC 在它的付费企业版里；Zuplo 是 SaaS 托管。这个项目走另一条路——免费、自托管、单进程、air-gapped 能用，MCP 数据面的 OAuth 2.1 resource server 和工具变更可见性都内置。

## 8. 项目结构

```
openapi-mcp-hub/
├── README.md / README.zh-CN.md / USAGE.md / CHANGELOG.md
├── SECURITY.md                 # 漏洞报告渠道、in/out-of-scope、运维加固清单
├── CONTRIBUTING.md / Makefile  # make install/test/lint/check/build
├── pyproject.toml
├── docker-compose.yml          # 端口 20033 + data 卷
├── src/openapi_mcp_hub/
│   ├── main.py                 # FastAPI 入口、启动恢复
│   ├── admin.py                # 管理面 /services CRUD
│   ├── registry.py             # 注册表：读写锁 + 落盘 + 恢复
│   ├── mcp_host.py             # 每服务 MCP 实例，挂载 /mcp/{name}，数据面认证在这层
│   ├── jwt_auth.py             # JWT 签发/验签（内置 HS256 + 外部 JWKS）
│   ├── loader.py               # spec 加载/校验/$ref 解析（含循环检测）
│   ├── models.py               # 中间模型：ApiEndpoint / ApiParam / DataPlaneAuthn …
│   ├── extractor.py            # 接口提取
│   ├── generator.py            # ApiEndpoint → MCP Tool
│   ├── annotations.py          # 按 HTTP 语义推断 MCP 工具标注（只读/破坏性/幂等）
│   ├── compact.py              # 工具描述与 schema 的体积压缩（零依赖、零配置）
│   ├── invoker.py              # per-service httpx + 超时 + 错误映射 + oauth2 token
│   ├── db.py                   # 数据库注册源：内省 + 只读执行器
│   ├── store.py                # 落盘后端：文件 / PostgreSQL
│   ├── templates.py            # 响应模板渲染
│   ├── config.py               # 环境变量与网关级配置
│   └── static/index.html       # 控制台（单文件）
├── examples/
│   ├── register-petstore.json  # 注册请求示例
│   └── petstore-openapi.json
└── tests/
    ├── fixtures/               # $ref、循环引用、deprecated、巨型 spec 样例
    ├── test_loader.py / test_extractor.py / test_generator.py / test_invoker.py
    ├── test_admin.py / test_jwt_auth.py / test_db_mcp.py / test_db_mysql.py
    └── test_e2e.py             # 注册 → MCP 客户端 → mock API 全链路
```

## 9. 技术选型与环境要求

- Python ≥ 3.10；`fastapi` + `uvicorn`、`mcp`（官方 SDK，FastMCP）、`httpx`、`pydantic`、`PyYAML`、`PyJWT[crypto]`；测试 `pytest` + `respx`；
- 网关环境变量：

| 变量 | 说明 |
|---|---|
| `OPENAPI_MCP_HUB_ADMIN_TOKEN` | 管理面 token（必填） |
| `OPENAPI_MCP_HUB_DATA_DIR` | 注册数据目录，默认 `./data` |
| `OPENAPI_MCP_HUB_HOST_ALLOWLIST` | 允许转发的目标主机/网段，逗号分隔；空 = 不限制 |
| `OPENAPI_MCP_HUB_JWT_SECRET` | 内置 JWT 签发的密钥（仅 authn 为内置 jwt 模式时需要） |
| 各服务凭证 | 由注册时的 `token_env` / `url_env` 引用，如 `GRANDE_TOKEN` |

建议给网关一个内网域名（比如 `mcp.internal.example`）：用裸 IP 的话，迁移时所有 agent 配置都得改。

## 10. Roadmap

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 网关骨架：FastAPI + Registry（落盘/恢复）+ Loader/Extractor/Generator + 管理面 CRUD + `/mcp/{name}` 挂载 + 双层鉴权 | ✅ 注册 petstore 后 MCP 客户端能列工具、调工具，e2e 通过 |
| M2 | 调用链加固：响应模板、SSRF 加固（DNS 核验/重定向逐跳/mapped IPv6）、结构化错误模型、Swagger 2.0 + OpenAPI 3.1、br/zstd 解压 | ✅ 121 项测试通过 |
| M3 | 可观测与 DX：调用日志、预览端点、allowed_tools、表单/multipart、schema default 填充、模板 `\|\|` 默认值 | ✅ 138 项测试通过；对照 Unla 吸收 6 项，多租户/Redis/OTel/Prometheus/版本管理明确不做 |
| M4 | SSE 兼容端点、健康探测、Docker 部署 | ✅ 双 transport、HEAD/GET 探测 + 凭证自检、镜像与容器全链路验证；剩独立 server 导出、HA |
| M5-1 | 数据库 → MCP：PostgreSQL / MySQL 8，六层强制只读 | ✅ 三件套工具上线；M5-2 per_table 与 M5-3 写模式评审后搁置 |
| v0.3.0 | oauth2 client_credentials 目标认证、refresh 热更新、调用统计、控制台 auth 表单修复 | ✅ 163 项测试通过 |
| v0.4.0 | JWT 数据面认证（OAuth 2.1 resource server）、tool poisoning 漂移 diff | ✅ 177 项测试通过 |
| v0.5.0 | 工具安全标注、描述压缩、可自纠正错误、CLI 参数、多架构镜像 | ✅ 247 项测试通过 |
| v0.6.0 | 渐进式工具发现（search_tools + advertise limit）、`ttlMs`/`cacheScope`、确定性顺序 | ✅ 266 项测试通过 |
| v0.6.1 | stdio 桥（只吃 stdio 的客户端 + 官方 Registry 收录前置） | ✅ 279 项测试通过 |
| v0.5.0 | 面向 agent 的工具质量：安全标注、描述/schema 压缩、可自纠正错误提示、CLI 参数、多架构镜像 | ✅ 247 项测试通过 |

往后：官方 MCP registry 发布、工具语义搜索/路由（工具很多时让 agent 只看到需要的那几个）、控制台的 jwt 配置界面。

**目前尚未实现**（不要按这些做规划）：熔断、per-service 并发限流 / 配额、HA 与多副本协同、Prometheus 指标、数据库写操作、per_table 表级工具、api_key 过期。

**刻意永久不做**：任何把网关变成“平台”的东西——RBAC/SSO、多副本协调、指标全家桶、审计库。它们都在拿默认路径的轻量去换前置反代已经解决的问题。当前的隔离手段是「每服务独立 httpx client + 独立超时 + 连接池上限 + SSRF allowlist」，全部在单进程内。

## 11. 贡献与联系

- 分支策略：`main` 为主干，功能开发用 `feat/*`，合并前过单测；
- 提 issue 前看一眼模板（bug_report / feature_request），CONTRIBUTING.md 里有开发环境说明。
