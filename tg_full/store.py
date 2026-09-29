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
    """
    -- Telegram media MaiBot has seen, keyed by Telegram file and by the bytes MaiBot got.
    CREATE TABLE media (
        file_key        TEXT NOT NULL,     -- "photo:<id>" / "document:<id>", stable across chats
        variant         TEXT NOT NULL,     -- what MaiBot received: full / thumb / gif
        sha256          TEXT NOT NULL,     -- hash of those bytes = MaiBot's image hash
        kind            TEXT NOT NULL,     -- photo, sticker, animation, video, ...
        ref_type        TEXT NOT NULL,     -- photo / document
        media_id        INTEGER NOT NULL,
        access_hash     INTEGER NOT NULL,
        file_reference  BLOB NOT NULL,
        origin_chat     INTEGER,
        origin_msg      INTEGER,
        emoji           TEXT,
        set_id          INTEGER,
        set_access_hash INTEGER,
        updated         INTEGER NOT NULL,
        PRIMARY KEY (file_key, variant)
    );
    CREATE INDEX media_sha ON media (sha256);
    CREATE INDEX media_emoji ON media (emoji);
    CREATE TABLE link_cache (
        url     TEXT PRIMARY KEY,
        data    TEXT NOT NULL,  -- JSON {title, description, site_name}; {} when nothing was found
        fetched INTEGER NOT NULL
    );
    """,
    """
    -- What MaiBot was told about each message, to describe later edits and deletions.
    ALTER TABLE msg_meta ADD COLUMN sender_name TEXT;
    ALTER TABLE msg_meta ADD COLUMN text TEXT;
    CREATE INDEX msg_meta_msg ON msg_meta (msg_id);
    """,
    """
    ALTER TABLE msg_meta ADD COLUMN topic_id INTEGER;
    """,
]

MEDIA_COLUMNS = (
    "file_key", "variant", "sha256", "kind", "ref_type", "media_id", "access_hash", "file_reference",
    "origin_chat", "origin_msg", "emoji", "set_id", "set_access_hash", "updated",
)


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
        topic_id: int | None = None,
        text: str | None = None,
    ) -> None:
        await self.execute(
            "INSERT INTO msg_meta (chat_id, msg_id, sender_id, sender_is_bot, is_outgoing, routed, date, topic_id, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, msg_id) DO UPDATE SET "
            "sender_id = excluded.sender_id, sender_is_bot = excluded.sender_is_bot, "
            "is_outgoing = excluded.is_outgoing, routed = max(routed, excluded.routed), "
            "topic_id = coalesce(excluded.topic_id, topic_id), text = coalesce(excluded.text, text)",
            (chat_id, msg_id, sender_id, int(sender_is_bot), int(is_outgoing), int(routed), int(date or time.time()),
             topic_id, text),
        )

    async def mark_routed(self, chat_id: int, msg_id: int, sender_name: str | None = None, text: str | None = None) -> None:
        await self.execute(
            "UPDATE msg_meta SET routed = 1, sender_name = coalesce(?, sender_name), text = coalesce(?, text) "
            "WHERE chat_id = ? AND msg_id = ?",
            (sender_name, text, chat_id, msg_id),
        )

    async def set_snapshot(self, chat_id: int, msg_id: int, text: str) -> None:
        await self.execute("UPDATE msg_meta SET text = ? WHERE chat_id = ? AND msg_id = ?", (text, chat_id, msg_id))

    async def forget_message(self, chat_id: int, msg_id: int) -> None:
        await self.execute("DELETE FROM msg_meta WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id))

    async def find_common_box(self, msg_ids: Sequence[int]) -> list[tuple[int, int]]:
        """``(chat_id, msg_id)`` of private-chat / basic-group messages with these ids.

        User accounts share one message id sequence across those chats, so Telegram reports their
        deletions without a chat id. Channels and supergroups (``-100…`` ids) have their own.
        """
        if not msg_ids:
            return []
        placeholders = ", ".join("?" for _ in msg_ids)
        rows = await self.fetchall(
            f"SELECT chat_id, msg_id FROM msg_meta WHERE msg_id IN ({placeholders}) AND chat_id > -1000000000000",
            tuple(msg_ids),
        )
        return [(row[0], row[1]) for row in rows]

    async def recent_routed(self, since: float, per_chat: int) -> dict[int, list[int]]:
        """Recently routed message ids per chat (newest first), for history polling."""
        rows = await self.fetchall(
            "SELECT chat_id, msg_id FROM msg_meta WHERE routed = 1 AND date >= ? ORDER BY chat_id, msg_id DESC",
            (int(since),),
        )
        result: dict[int, list[int]] = {}
        for chat_id, msg_id in rows:
            ids = result.setdefault(chat_id, [])
            if len(ids) < per_chat:
                ids.append(msg_id)
        return result

    async def is_known_to_core(self, chat_id: int, msg_id: int, max_age_seconds: int = 3600) -> bool:
        """Whether MaiBot has this message in its recent context (routed or sent by it, and recent)."""
        row = await self.fetchone(
            "SELECT 1 FROM msg_meta WHERE chat_id = ? AND msg_id = ? AND routed = 1 AND date >= ?",
            (chat_id, msg_id, int(time.time()) - max_age_seconds),
        )
        return row is not None

    async def get_message_meta(self, chat_id: int, msg_id: int) -> dict[str, Any] | None:
        row = await self.fetchone(
            "SELECT sender_id, sender_is_bot, is_outgoing, date, routed, sender_name, text, topic_id FROM msg_meta "
            "WHERE chat_id = ? AND msg_id = ?",
            (chat_id, msg_id),
        )
        if row is None:
            return None
        return {
            "sender_id": row[0], "sender_is_bot": bool(row[1]), "is_outgoing": bool(row[2]), "date": row[3],
            "routed": bool(row[4]), "sender_name": row[5], "text": row[6], "topic_id": row[7],
        }

    async def prune_messages(self, older_than_seconds: int) -> int:
        return await self.execute("DELETE FROM msg_meta WHERE date < ?", (int(time.time()) - older_than_seconds,))

    # ---- media -------------------------------------------------------------------------

    async def remember_media(self, row: dict[str, Any]) -> None:
        values = {**row, "updated": int(time.time())}
        placeholders = ", ".join("?" for _ in MEDIA_COLUMNS)
        await self.execute(
            f"INSERT OR REPLACE INTO media ({', '.join(MEDIA_COLUMNS)}) VALUES ({placeholders})",
            tuple(values.get(column) for column in MEDIA_COLUMNS),
        )

    async def get_media(self, file_key: str, variant: str) -> dict[str, Any] | None:
        row = await self.fetchone(
            f"SELECT {', '.join(MEDIA_COLUMNS)} FROM media WHERE file_key = ? AND variant = ?", (file_key, variant)
        )
        return dict(zip(MEDIA_COLUMNS, row, strict=True)) if row else None

    async def media_by_sha(self, sha256: str) -> dict[str, Any] | None:
        row = await self.fetchone(
            f"SELECT {', '.join(MEDIA_COLUMNS)} FROM media WHERE sha256 = ? ORDER BY updated DESC LIMIT 1", (sha256,)
        )
        return dict(zip(MEDIA_COLUMNS, row, strict=True)) if row else None

    async def update_file_reference(self, file_key: str, file_reference: bytes) -> None:
        await self.execute(
            "UPDATE media SET file_reference = ?, updated = ? WHERE file_key = ?",
            (file_reference, int(time.time()), file_key),
        )

    # ---- link previews -----------------------------------------------------------------

    async def get_link(self, url: str, max_age_seconds: int) -> dict[str, Any] | None:
        row = await self.fetchone(
            "SELECT data FROM link_cache WHERE url = ? AND fetched >= ?", (url, int(time.time()) - max_age_seconds)
        )
        return None if row is None else json.loads(row[0])

    async def set_link(self, url: str, data: dict[str, Any]) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO link_cache (url, data, fetched) VALUES (?, ?, ?)",
            (url, json.dumps(data, ensure_ascii=False), int(time.time())),
        )
