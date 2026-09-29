"""Adapter-local persistent storage.

MaiBot's ``ctx.db`` only exposes the host's fixed models, so adapter state lives in its own
SQLite file under ``ctx.paths.data_dir``. All calls run in a worker thread to keep the event
loop free.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Each entry migrates the schema from version ``index`` to ``index + 1``.
_MIGRATIONS: list[str] = [
    """
    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE msg_meta (
        chat_id       INTEGER NOT NULL,
        msg_id        INTEGER NOT NULL,
        sender_id     INTEGER,
        sender_is_bot INTEGER NOT NULL DEFAULT 0,
        is_outgoing   INTEGER NOT NULL DEFAULT 0,
        routed        INTEGER NOT NULL DEFAULT 0,  -- delivered to MaiBot (inbound) or sent by MaiBot
        date          INTEGER NOT NULL,
        PRIMARY KEY (chat_id, msg_id)
    );
    CREATE INDEX msg_meta_chat_date ON msg_meta (chat_id, date);
    """,
]


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(_MIGRATIONS)):
            conn.executescript(f"BEGIN;{_MIGRATIONS[index]}PRAGMA user_version={index + 1};COMMIT;")
        self._conn = conn

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.close)

    def _run(self, sql: str, params: Sequence[Any], fetch: str) -> Any:
        if self._conn is None:
            raise RuntimeError("store is not open")
        with self._lock:
            cursor = self._conn.execute(sql, params)
            if fetch == "one":
                return cursor.fetchone()
            if fetch == "all":
                return cursor.fetchall()
            return cursor.rowcount

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await asyncio.to_thread(self._run, sql, params, "none")

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        return await asyncio.to_thread(self._run, sql, params, "one")

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return await asyncio.to_thread(self._run, sql, params, "all")

    # ---- key/value -------------------------------------------------------------------

    async def get_json(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return default if row is None else json.loads(row[0])

    async def set_json(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---- message metadata ------------------------------------------------------------

    async def record_message(
        self,
        chat_id: int,
        msg_id: int,
        sender_id: int | None,
        sender_is_bot: bool,
        is_outgoing: bool,
        routed: bool,
        date: float | None = None,
    ) -> None:
        await self.execute(
            "INSERT INTO msg_meta (chat_id, msg_id, sender_id, sender_is_bot, is_outgoing, routed, date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, msg_id) DO UPDATE SET "
            "sender_id = excluded.sender_id, sender_is_bot = excluded.sender_is_bot, "
            "is_outgoing = excluded.is_outgoing, routed = max(routed, excluded.routed)",
            (chat_id, msg_id, sender_id, int(sender_is_bot), int(is_outgoing), int(routed), int(date or time.time())),
        )

    async def mark_routed(self, chat_id: int, msg_id: int) -> None:
        await self.execute("UPDATE msg_meta SET routed = 1 WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id))

    async def is_known_to_core(self, chat_id: int, msg_id: int, max_age_seconds: int = 3600) -> bool:
        """Whether MaiBot has this message in its recent context (routed or sent by it, and recent)."""
        row = await self.fetchone(
            "SELECT 1 FROM msg_meta WHERE chat_id = ? AND msg_id = ? AND routed = 1 AND date >= ?",
            (chat_id, msg_id, int(time.time()) - max_age_seconds),
        )
        return row is not None

    async def get_message_meta(self, chat_id: int, msg_id: int) -> dict[str, Any] | None:
        row = await self.fetchone(
            "SELECT sender_id, sender_is_bot, is_outgoing, date FROM msg_meta WHERE chat_id = ? AND msg_id = ?",
            (chat_id, msg_id),
        )
        if row is None:
            return None
        return {"sender_id": row[0], "sender_is_bot": bool(row[1]), "is_outgoing": bool(row[2]), "date": row[3]}

    async def prune_messages(self, older_than_seconds: int) -> int:
        return await self.execute("DELETE FROM msg_meta WHERE date < ?", (int(time.time()) - older_than_seconds,))
