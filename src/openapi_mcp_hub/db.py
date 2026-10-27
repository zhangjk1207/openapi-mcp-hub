"""DB → MCP（M5-1）：PostgreSQL / MySQL 只读内省与执行。

双方言轻量分叉（不上 SQLAlchemy）：dialect 由 DSN 前缀推断
（postgresql:// → asyncpg；mysql:// → aiomysql），注册接口与工具形态完全一致。

安全六层（详见 docs/PLAN-DB-MCP.md §5）：
1. 凭证：DSN 只经环境变量引用（url_env），落盘/日志/错误全链路不出现密码
2. 只读三重强制：READ ONLY 事务 + 语句白名单（首 token 仅 SELECT/WITH/EXPLAIN，
   禁多语句）+ 建议运维侧用只读账号（纵深防御，文档注明）
3. 资源限制：池级 statement_timeout + 行数上限（多取 1 行判定截断）+ 64KB 输出截断
4. 连接池：每服务独立（min 1 / max 5）
5. 暴露面：include/exclude_tables 过滤；只内省目标 schema 的用户表/视图
6. 审计：SQL 摘要进 calls 日志（复用，不记结果集）
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg

from .invoker import InvokeResult
from .models import DbSourceConfig, ToolDef

logger = logging.getLogger(__name__)

# 语句白名单：括号开头如 "(SELECT 1)" 也放行（lstrip "(" 后再判首 token）
_ALLOWED_HEADS = {"SELECT", "WITH", "EXPLAIN"}

# 内省时始终排除的系统表模式（叠加用户 exclude）
_DEFAULT_EXCLUDE = ["pg_*", "information_schema*"]


# ---------------------------------------------------------------- 方言

def dialect_of(dsn: str) -> str:
    """DSN 前缀推断方言：pg / mysql（注册期校验，错误信息可诊断）。"""
    scheme = urlparse(dsn).scheme.lower()
    if scheme in ("postgresql", "postgres"):
        return "pg"
    if scheme in ("mysql", "mariadb"):
        return "mysql"
    raise ValueError(
        f"不支持的 DSN 前缀: {scheme or '(空)'}（支持 postgresql:// 与 mysql://）"
    )


def check_driver(dsn: str) -> None:
    """驱动可用性预检（mysql 驱动缺失时给出安装指引，而非裸 ImportError）。"""
    if dialect_of(dsn) == "mysql":
        try:
            import aiomysql  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ValueError(
                "网关未安装 MySQL 驱动：pip install aiomysql（或 pip install -e .）"
            ) from exc


async def create_db_pool(dsn: str, timeout_s: float) -> Any:
    """按方言建连接池：pg=asyncpg（server_settings 超时）；mysql=aiomysql
    （init_command 注入 max_execution_time，仅作用于 SELECT，恰合只读白名单；
    charset 强制 utf8mb4 防中文注释乱码；需 MySQL 8.0+）。"""
    if dialect_of(dsn) == "mysql":
        import aiomysql

        # aiomysql 不接受 URL DSN：解析为 host/port/user/password/db（密码 URL 转义还原）
        u = urlparse(dsn)
        return await aiomysql.create_pool(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=u.username or "root",
            password=unquote(u.password or ""),
            db=u.path.lstrip("/"),
            minsize=1,
            maxsize=5,
            charset="utf8mb4",
            init_command=f"SET SESSION max_execution_time={int(timeout_s * 1000)}",
            autocommit=True,  # 事务由 _query 显式管理（START TRANSACTION READ ONLY）
        )
    import asyncpg

    return await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        server_settings={"statement_timeout": str(int(timeout_s * 1000))},
    )


async def close_db_pool(pool: Any) -> None:
    if hasattr(pool, "executemany"):  # asyncpg：单步 close
        await pool.close()
    else:  # aiomysql：close + wait_closed
        pool.close()
        await pool.wait_closed()


# ---------------------------------------------------------------- 内省

@dataclass
class ColumnMeta:
    name: str
    type: str
    nullable: bool
    comment: str = ""


@dataclass
class TableMeta:
    name: str
    kind: str  # table / view
    comment: str = ""
    est_rows: int = 0
    columns: list[ColumnMeta] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)


async def introspect(
    pool: Any, dialect: str, schema: str, include: list[str], exclude: list[str]
) -> list[TableMeta]:
    """内省目标 schema（MySQL 语义为 database）下的表/视图、列、注释、主键、行数估计。"""
    if dialect == "mysql":
        tables_meta = await _introspect_mysql(pool, schema)
    else:
        tables_meta = await _introspect_pg(pool, schema)

    patterns = list(exclude) + _DEFAULT_EXCLUDE
    out: list[TableMeta] = []
    for t in tables_meta:
        if include and not any(fnmatch.fnmatch(t.name, p) for p in include):
            continue
        if any(fnmatch.fnmatch(t.name, p) for p in patterns):
            continue
        out.append(t)
    return out


async def _introspect_pg(pool: Any, schema: str) -> list[TableMeta]:
    """pg：pg_class/pg_attribute + obj_description/col_description。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.relname AS name,
                   CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'view'
                        ELSE 'table' END AS kind,
                   COALESCE(obj_description(c.oid), '') AS comment,
                   GREATEST(c.reltuples::bigint, 0) AS est_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1 AND c.relkind IN ('r', 'v', 'm')
            ORDER BY c.relname
            """,
            schema,
        )
        tables: list[TableMeta] = []
        for r in rows:
            name = r["name"]
            cols = await conn.fetch(
                """
                SELECT a.attname AS name,
                       format_type(a.atttypid, a.atttypmod) AS type,
                       NOT a.attnotnull AS nullable,
                       COALESCE(col_description(a.attrelid, a.attnum), '') AS comment
                FROM pg_attribute a
                WHERE a.attrelid = $1::regclass AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY a.attnum
                """,
                f'"{schema}"."{name}"',
            )
            pk = await conn.fetch(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid
                     AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = $1::regclass AND i.indisprimary
                ORDER BY array_position(i.indkey, a.attnum)
                """,
                f'"{schema}"."{name}"',
            )
            tables.append(
                TableMeta(
                    name=name,
                    kind=r["kind"],
                    comment=r["comment"],
                    est_rows=int(r["est_rows"]),
                    columns=[ColumnMeta(**dict(c)) for c in cols],
                    primary_key=[p["attname"] for p in pk],
                )
            )
    return tables


async def _introspect_mysql(pool: Any, schema: str) -> list[TableMeta]:
    """MySQL 8：information_schema（TABLE_COMMENT/COLUMN_COMMENT 原生注释）。"""
    import aiomysql

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT TABLE_NAME AS name,
                       IF(TABLE_TYPE = 'VIEW', 'view', 'table') AS kind,
                       IFNULL(TABLE_COMMENT, '') AS comment,
                       CAST(IFNULL(TABLE_ROWS, 0) AS SIGNED) AS est_rows
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME
                """,
                (schema,),
            )
            rows = await cur.fetchall()
            tables: list[TableMeta] = []
            for r in rows:
                cols_sql = await _mysql_query(
                        cur,
                        """
                        SELECT COLUMN_NAME AS name, COLUMN_TYPE AS type,
                               (IS_NULLABLE = 'YES') AS nullable,
                               IFNULL(COLUMN_COMMENT, '') AS comment
                        FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY ORDINAL_POSITION
                        """,
                        (schema, r["name"]),
                )
                pk_rows = await _mysql_query(
                    cur,
                    """
                    SELECT COLUMN_NAME AS name
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                      AND CONSTRAINT_NAME = 'PRIMARY'
                    ORDER BY ORDINAL_POSITION
                    """,
                    (schema, r["name"]),
                )
                # IS_NULLABLE='YES' → 1；est_rows 可能为 None
                tables.append(
                    TableMeta(
                        name=r["name"],
                        kind=r["kind"],
                        comment=r["comment"] or "",
                        est_rows=int(r["est_rows"] or 0),
                        columns=[
                            ColumnMeta(
                                name=c["name"],
                                type=c["type"],
                                nullable=bool(c["nullable"]),
                                comment=c["comment"] or "",
                            )
                            for c in cols_sql
                        ],
                        primary_key=[p["name"] for p in pk_rows],
                    )
                )
    return tables


async def _mysql_query(cur: Any, sql: str, args: tuple) -> list[dict]:
    """在已有 cursor 上执行并 fetchall（DictCursor 行已为 dict）。"""
    await cur.execute(sql, args)
    return list(await cur.fetchall())


# ---------------------------------------------------------------- 工具生成

def _table_summary_line(t: TableMeta) -> str:
    cols = ", ".join(
        f"{c.name} {c.type}" + (" PK" if c.name in t.primary_key else "")
        for c in t.columns
    )
    note = f"  -- {t.comment}" if t.comment else ""
    return f"- {t.name}({cols}){note}"


def build_db_tools(catalog: list[TableMeta], cfg: DbSourceConfig) -> list[ToolDef]:
    """通用三件套（tool_style=general）：工具数恒定，表多不爆炸。

    list_tables 的返回里就带全部表名+注释，query 的 description 里带
    schema 摘要 —— LLM 不 describe 也能直接盲写 SQL。
    """
    tables_line = "\n".join(_table_summary_line(t) for t in catalog) or "（无匹配表）"

    # DB 三件套均为只读（执行层用 READ ONLY 事务兜底），固定域（单个库）=> 封闭世界。
    # 显式标注让 agent 可以直接执行而无需人工确认。
    readonly_annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }

    list_tools = ToolDef(
        name="list_tables",
        description=(
            f"列出数据库 schema `{cfg.db_schema}` 中的表和视图"
            f"（含列清单、主键、注释、行数估计）。当前共 {len(catalog)} 个。"
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        db_action="list_tables",
        annotations=readonly_annotations,
    )
    describe = ToolDef(
        name="describe_table",
        description="查看单张表/视图的完整结构：列名、类型、可空、注释、主键、行数估计。",
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "表名（来自 list_tables 的结果）",
                }
            },
            "required": ["table"],
            "additionalProperties": False,
        },
        db_action="describe_table",
        annotations=readonly_annotations,
    )
    query = ToolDef(
        name="query",
        description=(
            f"在只读事务中执行单条 PostgreSQL 查询（仅 SELECT / WITH / EXPLAIN，"
            f"禁止多语句与任何写操作）。\n\nschema `{cfg.db_schema}` 概览：\n{tables_line}\n\n"
            f"返回 JSON：{{rows, row_count, truncated, elapsed_ms}}；"
            f"最多 {cfg.max_rows} 行，语句超时 {cfg.timeout_s}s。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "单条 SELECT/WITH/EXPLAIN 语句",
                }
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
        db_action="query",
        annotations=readonly_annotations,
    )
    return [list_tools, describe, query]


# ---------------------------------------------------------------- 执行器

class DbExecutor:
    """只读执行器：invoke 接口与 Invoker 对齐（返回 InvokeResult），mcp_host 统一后处理。"""

    def __init__(self, cfg: DbSourceConfig, dsn: str) -> None:
        self.cfg = cfg
        self._dsn = dsn
        self.dialect = dialect_of(dsn)
        self._pool: Any = None
        # 注册期内省结果（只读快照；结构漂移需 PUT 重注册，M5-4）
        self._catalog: list[TableMeta] = []

    def bind_catalog(self, catalog: list[TableMeta]) -> None:
        self._catalog = catalog

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await create_db_pool(self._dsn, self.cfg.timeout_s)

    async def close(self) -> None:
        if self._pool is not None:
            await close_db_pool(self._pool)
            self._pool = None

    # ------------------------------------------------------------ 入口

    async def invoke(self, tool: ToolDef, arguments: dict[str, Any]) -> InvokeResult:
        try:
            await self.connect()
            if tool.db_action == "list_tables":
                return self._ok(
                    {
                        "schema": self.cfg.db_schema,
                        "tables": [
                            {
                                "name": t.name,
                                "kind": t.kind,
                                "comment": t.comment,
                                "est_rows": t.est_rows,
                                "columns": [
                                    {
                                        "name": c.name,
                                        "type": c.type,
                                        "nullable": c.nullable,
                                        "comment": c.comment,
                                    }
                                    for c in t.columns
                                ],
                                "primary_key": t.primary_key,
                            }
                            for t in self._catalog
                        ],
                    }
                )
            if tool.db_action == "describe_table":
                return self._describe(arguments)
            if tool.db_action == "query":
                return await self._query(arguments)
            return InvokeResult(ok=False, error=f"未知 db_action: {tool.db_action}", error_code=-32603)
        except ReadOnlyViolation as exc:
            # 词法层拦下的写语句/多语句：-32602，告诉 LLM 为什么被拒
            return InvokeResult(ok=False, error=str(exc), error_code=-32602)
        except asyncpg.PostgresError as exc:
            return InvokeResult(
                ok=False,
                error=f"PostgreSQL 错误 [{exc.sqlstate}] {str(exc).splitlines()[0][:300]}",
                error_code=-32000,
            )
        except Exception as exc:  # noqa: BLE001 连接错误/超时（pg+mysql）与兜底
            name = exc.__class__.__name__
            if name in ("TimeoutError", "TimeoutError") or "timeout" in str(exc).lower():
                code, msg = -32002, f"查询超时（>{self.cfg.timeout_s}s 已中断）: {str(exc)[:150]}"
            elif isinstance(exc, OSError) or "Connection" in name or "Operational" in name or "Interface" in name:
                code, msg = -32002, f"数据库连接错误: {str(exc)[:150]}"
            elif "Error" in name:  # pymysql.err.ProgrammingError 等：SQL 层错误
                code, msg = -32000, f"数据库错误 [{name}] {str(exc).splitlines()[0][:200]}"
            else:
                logger.exception("db 工具 %s 执行异常", tool.db_action)
                code, msg = -32603, f"网关内部错误: {exc}"
            return InvokeResult(ok=False, error=msg, error_code=code)

    async def ping(self) -> tuple[bool, str | None]:
        """健康探测：SELECT 1（双方言通用）。"""
        try:
            await self.connect()
            if self.dialect == "mysql":
                import aiomysql

                async with self._pool.acquire() as conn:
                    async with conn.cursor(aiomysql.DictCursor) as cur:
                        await cur.execute("SELECT 1 AS ok")
                        await cur.fetchall()
            else:
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    # ------------------------------------------------------------ 实现

    def _describe(self, arguments: dict[str, Any]) -> InvokeResult:
        table = str(arguments.get("table") or "")
        for t in self._catalog:
            if t.name == table:
                return self._ok(
                    {
                        "name": t.name,
                        "kind": t.kind,
                        "comment": t.comment,
                        "est_rows": t.est_rows,
                        "primary_key": t.primary_key,
                        "columns": [
                            {
                                "name": c.name,
                                "type": c.type,
                                "nullable": c.nullable,
                                "comment": c.comment,
                            }
                            for c in t.columns
                        ],
                    }
                )
        known = ", ".join(t.name for t in self._catalog) or "（无）"
        return InvokeResult(
            ok=False,
            error=f"表 {table} 不在本服务暴露范围内。可用表: {known}",
            error_code=-32602,
        )

    async def _query(self, arguments: dict[str, Any]) -> InvokeResult:
        sql = _validate_readonly_sql(str(arguments.get("sql") or ""))
        assert self._pool is not None
        start = time.monotonic()
        # 行数上界：外层包一层 LIMIT（有界的不仅是返回，还有内存）；
        # EXPLAIN 返回命令标签非行集，不包直接执行（双方言同构）
        head = sql.split(None, 1)[0].lstrip("(/").upper()
        if head != "EXPLAIN":
            sql = f"SELECT * FROM ({sql}) AS _a2m_sub LIMIT {self.cfg.max_rows + 1}"
        rows: list[Any]
        if self.dialect == "mysql":
            rows = await self._query_mysql(sql)
        else:
            rows = await self._query_pg(sql)
        truncated = len(rows) > self.cfg.max_rows
        rows = rows[: self.cfg.max_rows]
        return self._ok(
            {
                "rows": [dict(r) for r in rows],
                "row_count": len(rows),
                "truncated": truncated,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            }
        )

    async def _query_pg(self, sql: str) -> list[Any]:
        async with self._pool.acquire() as conn:
            # READ ONLY 事务：即便语句白名单被绕过，写操作也会被 pg 拒绝
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(sql)
        return list(rows)

    async def _query_mysql(self, sql: str) -> list[Any]:
        """MySQL 8：START TRANSACTION READ ONLY + DictCursor；显式回滚释放连接。"""
        import aiomysql

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    await cur.execute("START TRANSACTION READ ONLY")
                    await cur.execute(sql)
                    return list(await cur.fetchall())
                finally:
                    # 无论成败都结束事务（只读回滚无副作用），归还池前不留事务状态
                    with contextlib.suppress(Exception):
                        await cur.execute("ROLLBACK")

    @staticmethod
    def _ok(payload: dict[str, Any]) -> InvokeResult:
        return InvokeResult(ok=True, status_code=200, payload=payload)


class ReadOnlyViolation(ValueError):
    """语句不满足只读白名单（词法层拦截，映射 -32602）。"""


def _scan_outside_literals(s: str) -> tuple[bool, bool]:
    """词法扫描（跳过字符串/标识符字面量）：返回 (含注释, 字面量外有分号)。

    '...'（含 '' 转义）、"..."、`...` 内的内容不参与注释/分号判定——
    避免 WHERE name LIKE '%--%' 或 tags='a;b' 这类合法查询被误拦。
    """
    quotes = {"'", '"', "`"}
    i, n = 0, len(s)
    has_comment = semi = False
    while i < n:
        ch = s[i]
        if ch in quotes:
            q = ch
            i += 1
            while i < n:
                if s[i] == q:
                    if i + 1 < n and s[i + 1] == q:  # 转义（'' / "" / ``）
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if s.startswith("--", i) or s.startswith("/*", i):
            has_comment = True
            if s.startswith("--", i):  # 行注释：跳到行尾
                j = s.find("\n", i)
                i = n if j < 0 else j + 1
            else:  # 块注释
                j = s.find("*/", i + 2)
                i = n if j < 0 else j + 2
            continue
        if ch == ";":
            semi = True
        i += 1
    return has_comment, semi


def _validate_readonly_sql(sql: str) -> str:
    """词法校验：单语句 + 首 token 白名单 + 禁注释/EXPLAIN ANALYZE。

    写操作与多语句由词法层先拦（报 -32602 可指导 LLM 改写）；
    任何绕过由 READ ONLY 事务兜底（已实测 WITH...DELETE / FOR UPDATE 均被 pg 拒）。
    """
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise ReadOnlyViolation("SQL 为空")
    head = s.split(None, 1)[0].lstrip("(/").upper()
    if head not in _ALLOWED_HEADS:
        raise ReadOnlyViolation(
            f"只读模式仅允许 SELECT / WITH / EXPLAIN 开头的单条查询，收到: {head}"
        )
    # EXPLAIN ANALYZE 会真实执行底层查询（且不包 LIMIT），拒绝；纯 EXPLAIN 仅生成计划
    import re

    if re.match(r"(?is)^\(?\s*explain\s+analyze\b", s):
        raise ReadOnlyViolation(
            "不允许 EXPLAIN ANALYZE（会真实执行查询）；请使用纯 EXPLAIN 查看执行计划"
        )
    has_comment, semi = _scan_outside_literals(s)
    if semi:
        raise ReadOnlyViolation("仅允许单条语句（检测到字面量外的 ; 分隔的多条语句）")
    if has_comment:
        # 注释不参与执行，但会混淆调用日志审计（SQL 摘要语义被注释改变），保守拒绝
        raise ReadOnlyViolation("不允许包含 SQL 注释（-- 或 /* */）")
    return s


def dsn_safe_display(dsn: str) -> str:
    """DSN 脱敏展示：postgres://user:***@host:port/db（按 DSN 前缀显示方言）。"""
    try:
        u = urlparse(dsn)
        scheme = "mysql" if u.scheme.lower() in ("mysql", "mariadb") else "postgres"
        host = u.hostname or ""
        port = f":{u.port}" if u.port else ""
        user = f"{u.username}:***@" if u.username else ""
        path = u.path or ""
        return f"{scheme}://{user}{host}{port}{path}"
    except Exception:  # noqa: BLE001
        return "db:***"


def row_to_json_text(payload: Any) -> str:
    """db 结果序列化（datetime/Decimal/UUID → str）。"""
    return json.dumps(payload, ensure_ascii=False, default=str)
