# 规划：数据库 → MCP（M5）

> 状态：**M5-1 已交付**（v0.2.0；v0.2.1 扩展 MySQL 8+ 双方言，见 README 与 USAGE §一.6）；
> 本文档保留为设计依据与后续阶段（M5-2~4）规划。
> 实现与规划的小偏差：内省/工具生成/执行器收敛为单模块 `db.py`；
> 字段 `schema` 落盘键为 `db_schema`（pydantic 遮蔽规避，API 双名兼容）；
> 行数上界实现为「外层 SELECT * FROM (...) LIMIT N+1 包裹」（含 EXPLAIN 直通例外）。

## 1. 目标与场景

内部大量数据**只有库表、没有 REST API**（如 15433 上的 workflow、new_api_prod、data_storage_service 等库）。让 agent 能查这些数据，现状只能人工写接口或导 CSV。

目标：`POST /services` 除了贴 openapi.json，还能**贴一个数据库连接**——网关内省库表结构，生成 MCP 工具，照常挂到 `/mcp/{name}`，Claude Code 直接用。

```
现状：openapi.json ──POST /services──▶ /mcp/{name}（REST 工具）
新增：pg 连接(env) ──POST /services──▶ /mcp/{name}（数据查询工具）
```

## 2. 非目标（明确不做）

- ❌ 不做"写库"（MVP 纯只读；写模式是后续独立决策，见 §8）
- ~~不做多数据库方言抽象~~ → v0.2.1 已支持 MySQL 8+（轻量双驱动分叉：asyncpg/aiomysql，
  未引入 SQLAlchemy；方言由 DSN 前缀推断，注册接口与工具形态零差异）
- ❌ 不做 Text-to-SQL 的"智能层"（不做 schema 语义增强、不做向量检索；就是把结构给 LLM，让它自己写 SQL）
- ❌ 不做 BI/聚合缓存（每次查询直达数据库）

## 3. 注册形态（API 草案）

`spec` 与 `db` 二选一，其余字段（name/filters?/owner/api_key/落盘/恢复）全部复用：

```json
POST /services
{
  "name": "workflow-db",
  "db": {
    "url_env": "WORKFLOW_DB_URL",           // 凭证只引用环境变量名，永不落盘（与 auth 同原则）
    "schema": "public",                     // 默认 public
    "mode": "readonly",                     // MVP 只有 readonly
    "tool_style": "general",                // general | per_table，见 §4
    "include_tables": ["t_*"],              // 可选，fnmatch 通配
    "exclude_tables": ["*_audit", "secret_*"],
    "max_rows": 200,                        // 单次查询行数上限，默认 200
    "timeout_s": 15                         // 语句超时，默认 15s
  },
  "owner": "zhangjikang"
}
```

落盘 record 与现有结构同构（`db` 块替代 `spec` 块），重启恢复走同一套 `load_from_store`。

## 4. 工具形态：三种策略对比（核心决策）

| 策略 | 生成物 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| A. 通用三件套 | `list_tables` / `describe_table(table)` / `query(sql)` | 工具数恒定 3 个；LLM 按需探查；大小库通吃 | LLM 要多轮（先 list→describe→query）才出结果 | **默认**，表多的库 |
| B. 每表一工具 | `select_workflow_tasks(status?, limit?)…` | 一次调用直达；参数 schema 明确 | 50 表 = 50 工具，超 max_tools 且降低 LLM 选择准确率 | 小库（≤15 表） |
| C. 混合 | A + 指定热点表升级为 B | 兼顾 | 实现面最大 | 后期 |

**推荐：默认 A，`tool_style: "per_table"` 可选 B**（B 自带 include/exclude 与表数上限熔断）。

`describe_table` 的返回（列名/类型/注释/主外键/行数估计）同时写入 `query` 工具的 description —— LLM 不 describe 也能盲写 SQL 的成功率大幅提高。

## 5. 执行器与安全设计（六层防护，缺一不可）

数据库 ≠ API：API 暴露的是**定义好的能力**，DB 暴露的是**任意查询权**。安全是本特性的第一设计约束：

1. **凭证**：`url_env` 只存变量名；DSN（含密码）只活在网关进程环境，落盘/日志/错误信息全链路脱敏（复用现有 auth 原则）
2. **只读强制（三重）**：
   - 连接层：推荐运维直接给只读账号（纵深防御，文档写清楚）
   - 事务层：每查询 `BEGIN READ ONLY`，写语句直接报错
   - 语句层：词法校验——首 token 必须 `SELECT`/`WITH`/`EXPLAIN`，禁止多语句（`;` 后有内容即拒）
3. **资源限制**：`statement_timeout`（默认 15s）+ 行数上限（默认 200，超出截断并在结果里注明总行数）+ 输出字节上限（复用 64KB 截断）
4. **连接池**：每服务独立池（min 1 / max 5，与 PgStore 同规格），防 LLM 并发打爆库
5. **暴露面**：include/exclude_tables（默认排除 `pg_*` 系统表）；`secret`/`password`/`token` 等列名在 describe 输出里**打码提示**（值仍在 query 结果里——是否默认屏蔽敏感列是拍板项 §8）
6. **审计**：复用 calls 日志（记录 SQL 前 200 字符摘要 + 行数 + 耗时 + 状态）；网关日志不记结果集

**错误映射**（复用现有错误码体系）：SQL 语法错/权限错 → `-32000`（data.http_status 用 pg 错误码），超时 → `-32002`，凭证 env 缺失 → `-32001`，语句被安全策略拦截 → `-32602`（明确告诉 LLM 为什么被拒、怎么改）。

## 6. 与现有架构的整合点

| 现有模块 | 复用情况 |
|---|---|
| `/mcp/{name}` 挂载、X-Api-Key、SSE+streamable | ✅ 原样复用（McpHost 不感知工具来源） |
| 落盘/恢复（pg + 文件双后端） | ✅ 原样复用（record 多一个 `db` 块） |
| 调用日志 / 健康探测 / rotate-key / preview | ✅ 复用（health 对 DB 发 `SELECT 1`） |
| allowed_tools / max_tools | ✅ 复用 |
| 响应模板 | ⚠️ 可选支持（查询结果套模板意义不大，MVP 不做） |
| loader/extractor/generator（openapi 流水线） | ❌ 不复用，独立小流水线 |
| invoker | 🔀 扩展：`ToolDef` 加 kind 字段（http/db），invoker 按 kind dispatch 到新增的 `db_executor` |
| **新增** `db_introspect.py` | 读 pg 系统目录（information_schema/pg_catalog）：表/视图清单、列、类型、注释、PK/FK、行数估计 |
| **新增** `db_executor.py` | 只读事务 + 语句校验 + 限制 + 结果序列化（≈ invoker 的数据库版） |

改动集中在：models（+db 配置）、registry（_build_service 分支）、invoker（dispatch）、两个新模块。**数据面零改动**。

## 7. 里程碑切分

| 阶段 | 内容 | 预估 |
|---|---|---|
| **M5-1（MVP）** | pg 只读：注册（db 源）→ 内省 → 通用三件套工具 → 六层安全 → 落盘恢复/health/calls 全复用；集成测试用 openapi_mcp_hub_test 库建玩具表 | 1.5~2 天 |
| **M5-2** | `per_table` 表级工具模式（含 include/exclude/上限熔断）；视图支持；describe 结果注入 query 工具 description | 0.5~1 天 |
| **M5-3** | 写模式评估（默认关，白名单表 + `confirm` 必填参数 + 影响行数预览）；MySQL（SQLAlchemy 统一抽象，成本另评） | 另立规划 |
| **M5-4** | schema 漂移检测（表结构变了自动刷新工具 description，目前需 PUT 重注册）；PG 函数暴露为工具 | 按需 |

## 8. 待拍板决策点

1. **暴露策略默认值**：通用三件套（推荐）还是每表工具？取决于你的库平均表数——15433 上那几个库如果普遍 30+ 表，必须 A
2. **敏感列**：`query` 结果里的 `password`/`token`/`secret` 类列要不要默认打码（返 `***`）？打码防泄露但可能挡住正当用途（如排查配置）——推荐**默认打码 + `unmask_columns` 白名单显式开**
3. **SQL 直查 vs 只参数化工具**：直查灵活但依赖 LLM 自律（有六层防护兜底）；业界（postgres-mcp 等）主流是直查+只读。推荐直查
4. **写操作**：MVP 后是否需要？需要的话 M5-3 单独评审（这是整个项目风险最高的一步）
5. **多库**：一个注册 = 一个 DSN = 一个库（推荐，命名即边界）；跨库 join 不做

## 9. 业界参照

| 项目 | 做法 | 我们的差异 |
|---|---|---|
| crystaldba/postgres-mcp | untrusted 模式下受限 SQL + 索引调优 | 我们是**网关**：多库多服务统一端口/统一 key/统一观测，非单库单进程 |
| Docker MCP Gateway catalog | 纯元数据浏览（不执行 SQL） | 我们多一层受控执行（它安全上限低、能力下限也低） |
| Supabase MCP | 项目管理 + SQL | 面向其云平台；我们是自托管任意 pg |
| Unla | 无数据库能力 | 不参考 |

核心差异化：**注册即多租户 + 与 REST 服务同窗管理**——agent 在同一个网关里既能调 REST 工具又能查库，运维一套 key/日志/健康体系。

## 10. 风险与开放问题

- **拖库风险残留**：只读 + 行数上限能挡"一次拿全表"，但 LLM 可以循环分页拿完——若敏感库建议配 `max_rows` 更低 + 网络层限制（MCP 端点本来就有 per-service key，泄露面可控）
- **表结构进 description 的 token 成本**：50 列大表 description 会很长，M5-2 做"超长截断 + 分组描述"
- **LLM 写 SQL 的成功率**：内省信息越全成功率越高（注释/PK/FK 都给），坏 SQL 会消耗多轮——calls 日志可观测重试率，再决定要不要加 EXPLAIN 预检
