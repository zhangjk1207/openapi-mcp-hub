"""持久化层：服务注册记录的存取。

两个后端，Registry 无感切换（接口一致，方法全异步）：
- FileStore：data/services/*.json 原子写（默认；无外部依赖，本地开发/单机够用）
- PgStore：PostgreSQL JSONB（配 API_TO_MCP_DATABASE_URL 后启用）

PgStore 建表：
    services(name PK, title, base_url, api_key_hash, owner, created_at, updated_at, record JSONB)
    —— 冗余列方便 SQL 查询/运维排错；record 与文件模式结构完全一致（spec 原文、auth 引用、
    filters、模板、allowed_tools、api_key_hash……），两后端数据可互迁。

首次切换到 pg 时自动做一次性迁移：表为空 && 文件目录存在记录 → 导入（原文件保留不动，
回滚到文件模式只需去掉环境变量重启）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Store(Protocol):
    """持久化接口：Registry 只依赖这四个方法。"""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def save(self, record: dict[str, Any]) -> None: ...
    async def delete(self, name: str) -> None: ...
    async def load_all(self) -> list[dict[str, Any]]: ...


class FileStore:
    """文件后端：{data_dir}/services/{name}.json，tmp+rename 原子写。"""

    def __init__(self, services_dir: Path) -> None:
        self._dir = services_dir

    async def connect(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    async def close(self) -> None:
        return

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.json"

    async def save(self, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(record["name"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # 同目录 rename，原子

    async def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    async def load_all(self) -> list[dict[str, Any]]:
        if not self._dir.exists():
            return []
        records = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("持久化文件 %s 损坏，已跳过: %s", path, exc)
        return records


class PgStore:
    """PostgreSQL 后端：asyncpg 连接池 + JSONB 存储；建库建表由运维/部署脚本完成，
    表结构启动时 IF NOT EXISTS 兜底。"""

    def __init__(self, dsn: str, legacy_services_dir: Path | None = None) -> None:
        self._dsn = dsn
        self._legacy_dir = legacy_services_dir  # 首次迁移用
        self._pool: Any = None

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS services (
                    name         TEXT PRIMARY KEY,
                    title        TEXT,
                    base_url     TEXT,
                    api_key_hash TEXT NOT NULL,
                    owner        TEXT NOT NULL DEFAULT '',
                    created_at   TEXT,
                    updated_at   TEXT,
                    record       JSONB NOT NULL
                )
                """
            )
            # 老表升级（v0.1.x base_url NOT NULL）：db 源服务（M5）无 base_url
            await conn.execute(
                "ALTER TABLE services ALTER COLUMN base_url DROP NOT NULL"
            )
        migrated = await self._migrate_from_files()
        if migrated:
            logger.info("已从文件持久化迁移 %d 条服务记录到 PostgreSQL", migrated)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def save(self, record: dict[str, Any]) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO services (name, title, base_url, api_key_hash, owner,
                                      created_at, updated_at, record)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                ON CONFLICT (name) DO UPDATE SET
                    title = EXCLUDED.title,
                    base_url = EXCLUDED.base_url,
                    api_key_hash = EXCLUDED.api_key_hash,
                    owner = EXCLUDED.owner,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    record = EXCLUDED.record
                """,
                record["name"],
                record.get("title"),
                record.get("base_url"),
                record["api_key_hash"],
                record.get("owner", ""),
                record.get("created_at"),
                record.get("updated_at"),
                json.dumps(record, ensure_ascii=False, default=str),
            )

    async def delete(self, name: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM services WHERE name = $1", name)

    async def load_all(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT record FROM services ORDER BY name"
            )
        return [json.loads(r["record"]) for r in rows]

    async def _migrate_from_files(self) -> int:
        """一次性迁移：pg 表为空且遗留文件目录有记录 → 导入。原文件保留（可回滚）。"""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval("SELECT count(*) FROM services")
        if count:
            return 0
        if self._legacy_dir is None or not self._legacy_dir.exists():
            return 0
        legacy = FileStore(self._legacy_dir)
        imported = 0
        for record in await legacy.load_all():
            if not isinstance(record, dict) or "name" not in record:
                continue
            await self.save(record)
            imported += 1
        return imported
