"""Хранилище (SQLite/aiosqlite): юзеры, сообщения, копии, игноры, фильтры, права."""

from __future__ import annotations

import secrets
import time
from datetime import datetime
from typing import Any, Iterable, Optional

import aiosqlite

import config

ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    anon          TEXT UNIQUE NOT NULL,
    username_enc  TEXT,
    username_hash TEXT,
    name_enc      TEXT,
    lang          TEXT    NOT NULL DEFAULT 'ru',
    registered    INTEGER NOT NULL DEFAULT 0,

    tag           INTEGER NOT NULL DEFAULT 0,
    tag_link      INTEGER NOT NULL DEFAULT 0,
    custom_name   TEXT,
    prefix        TEXT,
    badge         INTEGER NOT NULL DEFAULT 1,

    protect       INTEGER NOT NULL DEFAULT 0,
    autodel       INTEGER NOT NULL DEFAULT 0,
    reaction      INTEGER NOT NULL DEFAULT 1,
    media_meta    INTEGER NOT NULL DEFAULT 1,
    autoedit      INTEGER NOT NULL DEFAULT 0,
    del_warning   INTEGER NOT NULL DEFAULT 0,
    specials      INTEGER NOT NULL DEFAULT 1,
    modernize     INTEGER NOT NULL DEFAULT 0,
    autoreply     INTEGER NOT NULL DEFAULT 1,
    spoiler_in    INTEGER NOT NULL DEFAULT 0,
    spoiler_out   INTEGER NOT NULL DEFAULT 0,
    keep_username INTEGER NOT NULL DEFAULT 1,
    pm_open       INTEGER NOT NULL DEFAULT 1,

    afk           INTEGER NOT NULL DEFAULT 0,
    afk_auto      INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    role          TEXT    NOT NULL DEFAULT 'user',

    warns         INTEGER NOT NULL DEFAULT 0,
    warn_cycles   INTEGER NOT NULL DEFAULT 0,
    streak        INTEGER NOT NULL DEFAULT 0,
    mute_until    INTEGER NOT NULL DEFAULT 0,
    mute_reason   TEXT,

    msgs          INTEGER NOT NULL DEFAULT 0,
    joined_at     INTEGER NOT NULL,
    last_msg_at   INTEGER NOT NULL DEFAULT 0,
    last_report   INTEGER NOT NULL DEFAULT 0,
    last_hash     TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_uhash ON users(username_hash);

CREATE TABLE IF NOT EXISTS messages (
    ref           INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT UNIQUE NOT NULL,
    author_id     INTEGER NOT NULL,
    author_msg_id INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    edited        INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    stub          INTEGER NOT NULL DEFAULT 0,
    reply_ref     INTEGER,
    recipients    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (author_id, author_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id, created_at);

CREATE TABLE IF NOT EXISTS copies (
    ref      INTEGER NOT NULL,
    chat_id  INTEGER NOT NULL,
    msg_id   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_copies_ref ON copies(ref);

CREATE TABLE IF NOT EXISTS pmmap (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    peer_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS pmblocks (
    owner_id INTEGER NOT NULL,
    peer_id  INTEGER NOT NULL,
    PRIMARY KEY (owner_id, peer_id)
);

CREATE TABLE IF NOT EXISTS ignores (
    owner_id INTEGER NOT NULL,
    peer_id  INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, peer_id)
);
CREATE INDEX IF NOT EXISTS idx_ignores_peer ON ignores(peer_id);

CREATE TABLE IF NOT EXISTS filters (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    kind     TEXT    NOT NULL,      -- word | regex | media
    value    TEXT    NOT NULL,
    added_at INTEGER NOT NULL,
    UNIQUE (owner_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_filters_owner ON filters(owner_id);

CREATE TABLE IF NOT EXISTS daily (
    user_id INTEGER NOT NULL,
    day     TEXT    NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily(day);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    name    TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rights (
    user_id     INTEGER NOT NULL,
    right       TEXT    NOT NULL,
    daily_limit INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, right)
);

CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    right   TEXT    NOT NULL,
    day     TEXT    NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, right, day)
);

CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id    INTEGER NOT NULL,
    target_id  INTEGER NOT NULL,
    ref        INTEGER,
    reason     TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id  INTEGER NOT NULL,
    author_id  INTEGER NOT NULL,
    reason     TEXT,
    duration   INTEGER NOT NULL,
    until      INTEGER NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vote_ballots (
    vote_id INTEGER NOT NULL,
    voter   INTEGER NOT NULL,
    value   INTEGER NOT NULL,
    PRIMARY KEY (vote_id, voter)
);

CREATE TABLE IF NOT EXISTS vote_msgs (
    vote_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    msg_id  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._settings: dict[str, str] = {}
        self._commands: dict[str, bool] = {}

    # ------------------------------------------------------------------ init

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        for key, value in config.DEFAULT_SETTINGS.items():
            await self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        for name in config.TOGGLEABLE_COMMANDS:
            await self.conn.execute(
                "INSERT OR IGNORE INTO commands (name, enabled) VALUES (?, 1)", (name,)
            )
        await self.conn.commit()
        await self.reload_cache()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def reload_cache(self) -> None:
        cur = await self.conn.execute("SELECT key, value FROM settings")
        self._settings = {r["key"]: r["value"] for r in await cur.fetchall()}
        cur = await self.conn.execute("SELECT name, enabled FROM commands")
        self._commands = {r["name"]: bool(r["enabled"]) for r in await cur.fetchall()}

    # -------------------------------------------------------------- settings

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, config.DEFAULT_SETTINGS.get(key, default))

    def get_int(self, key: str) -> int:
        try:
            return int(float(self.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    def get_float(self, key: str) -> float:
        try:
            return float(self.get(key, 0))
        except (TypeError, ValueError):
            return 0.0

    def get_bool(self, key: str) -> bool:
        return str(self.get(key, "0")).lower() in {"1", "true", "on", "yes", "вкл"}

    def all_settings(self) -> dict[str, str]:
        return dict(self._settings)

    async def set(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await self.conn.commit()
        self._settings[key] = str(value)

    # -------------------------------------------------------------- commands

    def command_enabled(self, name: str) -> bool:
        return self._commands.get(name, True)

    def commands(self) -> dict[str, bool]:
        return dict(self._commands)

    async def toggle_command(self, name: str) -> bool:
        new = not self.command_enabled(name)
        await self.conn.execute(
            "INSERT INTO commands (name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled",
            (name, int(new)),
        )
        await self.conn.commit()
        self._commands[name] = new
        return new

    # ----------------------------------------------------------------- users

    async def _free_anon(self) -> str:
        while True:
            anon = "".join(secrets.choice(ALPHABET) for _ in range(4))
            cur = await self.conn.execute("SELECT 1 FROM users WHERE anon=?", (anon,))
            if await cur.fetchone() is None:
                return anon

    async def ensure_user(self, user_id: int) -> dict[str, Any]:
        cur = await self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        if row:
            return dict(row)
        anon = await self._free_anon()
        role = "owner" if user_id == config.OWNER_ID else "user"
        await self.conn.execute(
            "INSERT INTO users (id, anon, joined_at, role) VALUES (?, ?, ?, ?)",
            (user_id, anon, int(time.time()), role),
        )
        await self.conn.commit()
        cur = await self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(await cur.fetchone())

    async def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_by_username_hash(self, digest: str) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE username_hash=?", (digest,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def ids_by_username_hashes(self, digests: Iterable[str]) -> set[int]:
        digests = list(digests)
        if not digests:
            return set()
        marks = ",".join("?" * len(digests))
        cur = await self.conn.execute(
            f"SELECT id FROM users WHERE username_hash IN ({marks})", digests
        )
        return {r["id"] for r in await cur.fetchall()}

    async def resolve(self, token: str) -> Optional[dict[str, Any]]:
        """Ищет юзера по числовому ID или по @username. Основной способ — реплай."""
        token = (token or "").strip()
        if not token:
            return None
        if token.lstrip("-").isdigit():
            return await self.get_user(int(token))
        if token.startswith("@"):
            import crypto

            return await self.get_by_username_hash(crypto.fingerprint(token))
        return None

    async def update(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id)
        )
        await self.conn.commit()

    async def toggle(self, user_id: int, field: str) -> int:
        await self.conn.execute(
            f"UPDATE users SET {field} = CASE {field} WHEN 0 THEN 1 ELSE 0 END WHERE id=?",
            (user_id,),
        )
        await self.conn.commit()
        cur = await self.conn.execute(f"SELECT {field} FROM users WHERE id=?", (user_id,))
        return (await cur.fetchone())[0]

    async def recipients(self, exclude: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE active=1 AND afk=0 AND registered=1 AND id<>?",
            (exclude,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def staff(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE role IN ('admin','owner') ORDER BY role DESC"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def stats(self) -> dict[str, int]:
        cur = await self.conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN active=1 AND registered=1 THEN 1 END) AS active,
                      SUM(CASE WHEN afk=1 THEN 1 END) AS afk,
                      SUM(CASE WHEN mute_until > strftime('%s','now') THEN 1 END) AS muted
               FROM users"""
        )
        row = await cur.fetchone()
        return {k: (row[k] or 0) for k in ("total", "active", "afk", "muted")}

    async def bump_daily(self, user_id: int) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        await self.conn.execute(
            "INSERT INTO daily (user_id, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
            (user_id, day),
        )
        await self.conn.commit()

    async def daily_counts(self, user_id: int) -> tuple[int, int]:
        """(всего сегодня, из них твоих)."""
        day = datetime.now().strftime("%Y-%m-%d")
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS total, "
            "COALESCE(SUM(CASE WHEN user_id=? THEN count END), 0) AS mine "
            "FROM daily WHERE day=?",
            (user_id, day),
        )
        row = await cur.fetchone()
        return row["total"], row["mine"]

    async def silent_users(self, edge_ts: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE active=1 AND afk=0 AND registered=1 "
            "AND last_msg_at > 0 AND last_msg_at < ?",
            (edge_ts,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def expired_mutes(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE mute_until > 0 AND mute_until <= ?",
            (int(time.time()),),
        )
        return [dict(r) for r in await cur.fetchall()]

    # -------------------------------------------------------------- messages

    async def add_message(
        self, author_id: int, author_msg_id: int, reply_ref: Optional[int] = None
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(9)
        await self.conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(token, author_id, author_msg_id, created_at, reply_ref) VALUES (?, ?, ?, ?, ?)",
            (token, author_id, author_msg_id, int(time.time()), reply_ref),
        )
        await self.conn.commit()
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE author_id=? AND author_msg_id=?",
            (author_id, author_msg_id),
        )
        return dict(await cur.fetchone())

    async def message_by_ref(self, ref: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM messages WHERE ref=?", (ref,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def message_by_token(self, token: str) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM messages WHERE token=?", (token,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def message_by_author_msg(self, author_id: int, msg_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE author_id=? AND author_msg_id=?",
            (author_id, msg_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def message_by_copy(self, chat_id: int, msg_id: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT m.* FROM copies c JOIN messages m ON m.ref = c.ref "
            "WHERE c.chat_id=? AND c.msg_id=?",
            (chat_id, msg_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def find_message(self, chat_id: int, msg_id: int) -> Optional[dict[str, Any]]:
        return (
            await self.message_by_copy(chat_id, msg_id)
            or await self.message_by_author_msg(chat_id, msg_id)
        )

    async def add_copies(self, ref: int, pairs: Iterable[tuple[int, int]]) -> None:
        await self.conn.executemany(
            "INSERT OR REPLACE INTO copies (ref, chat_id, msg_id) VALUES (?, ?, ?)",
            [(ref, chat_id, msg_id) for chat_id, msg_id in pairs],
        )
        await self.conn.commit()

    async def copies(self, ref: int) -> list[tuple[int, int]]:
        cur = await self.conn.execute(
            "SELECT chat_id, msg_id FROM copies WHERE ref=?", (ref,)
        )
        return [(r["chat_id"], r["msg_id"]) for r in await cur.fetchall()]

    async def copy_in_chat(self, ref: int, chat_id: int) -> Optional[int]:
        cur = await self.conn.execute(
            "SELECT msg_id FROM copies WHERE ref=? AND chat_id=? LIMIT 1", (ref, chat_id)
        )
        row = await cur.fetchone()
        return row["msg_id"] if row else None

    async def mark_message(self, ref: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(
            f"UPDATE messages SET {sets} WHERE ref=?", (*fields.values(), ref)
        )
        await self.conn.commit()

    async def drop_copies(self, ref: int) -> None:
        await self.conn.execute("DELETE FROM copies WHERE ref=?", (ref,))
        await self.conn.commit()

    async def last_messages(self, author_id: int, limit: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE author_id=? AND deleted=0 "
            "ORDER BY created_at DESC LIMIT ?",
            (author_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def stale_messages(self, edge_ts: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE stub=0 AND created_at < ?", (edge_ts,)
        )
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------- ЛС

    async def add_pm(self, chat_id: int, msg_id: int, peer_id: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO pmmap VALUES (?, ?, ?, ?)",
            (chat_id, msg_id, peer_id, int(time.time())),
        )
        await self.conn.commit()

    async def get_pm(self, chat_id: int, msg_id: int) -> Optional[int]:
        cur = await self.conn.execute(
            "SELECT peer_id FROM pmmap WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
        )
        row = await cur.fetchone()
        return row["peer_id"] if row else None

    async def pm_block(self, owner_id: int, peer_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO pmblocks VALUES (?, ?)", (owner_id, peer_id)
        )
        await self.conn.commit()

    async def pm_unblock(self, owner_id: int, peer_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM pmblocks WHERE owner_id=? AND peer_id=?", (owner_id, peer_id)
        )
        await self.conn.commit()

    async def pm_blocked(self, owner_id: int, peer_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM pmblocks WHERE owner_id=? AND peer_id=?", (owner_id, peer_id)
        )
        return await cur.fetchone() is not None

    async def pm_blocklist(self, owner_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT peer_id FROM pmblocks WHERE owner_id=?", (owner_id,)
        )
        return [r["peer_id"] for r in await cur.fetchall()]

    async def pm_block_count(self, owner_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM pmblocks WHERE owner_id=?", (owner_id,)
        )
        return (await cur.fetchone())["n"]

    # -------------------------------------------------------------- игноры

    async def ignore_add(self, owner_id: int, peer_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO ignores VALUES (?, ?, ?)",
            (owner_id, peer_id, int(time.time())),
        )
        await self.conn.commit()

    async def ignore_remove(self, owner_id: int, peer_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM ignores WHERE owner_id=? AND peer_id=?", (owner_id, peer_id)
        )
        await self.conn.commit()

    async def ignore_clear(self, owner_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM ignores WHERE owner_id=?", (owner_id,))
        await self.conn.commit()
        return cur.rowcount or 0

    async def ignore_list(self, owner_id: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT i.peer_id FROM ignores i "
            "WHERE i.owner_id=? ORDER BY i.added_at",
            (owner_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def is_ignored(self, owner_id: int, peer_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM ignores WHERE owner_id=? AND peer_id=?", (owner_id, peer_id)
        )
        return await cur.fetchone() is not None

    async def ignore_count(self, owner_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM ignores WHERE owner_id=?", (owner_id,)
        )
        return (await cur.fetchone())["n"]

    async def ignoring_author(self, author_id: int) -> set[int]:
        """Кто именно скрыл этого автора — одним запросом на всю рассылку."""
        cur = await self.conn.execute(
            "SELECT owner_id FROM ignores WHERE peer_id=?", (author_id,)
        )
        return {r["owner_id"] for r in await cur.fetchall()}

    # -------------------------------------------------------------- фильтры

    async def filter_add(self, owner_id: int, kind: str, value: str) -> bool:
        try:
            await self.conn.execute(
                "INSERT INTO filters (owner_id, kind, value, added_at) VALUES (?, ?, ?, ?)",
                (owner_id, kind, value, int(time.time())),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def filter_remove(self, owner_id: int, value: str) -> int:
        cur = await self.conn.execute(
            "DELETE FROM filters WHERE owner_id=? AND value=?", (owner_id, value)
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def filter_clear(self, owner_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM filters WHERE owner_id=?", (owner_id,))
        await self.conn.commit()
        return cur.rowcount or 0

    async def filter_list(self, owner_id: int) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM filters WHERE owner_id=? ORDER BY added_at", (owner_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def filter_count(self, owner_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM filters WHERE owner_id=?", (owner_id,)
        )
        return (await cur.fetchone())["n"]

    async def all_filters(self) -> dict[int, list[tuple[str, str]]]:
        cur = await self.conn.execute("SELECT owner_id, kind, value FROM filters")
        out: dict[int, list[tuple[str, str]]] = {}
        for row in await cur.fetchall():
            out.setdefault(row["owner_id"], []).append((row["kind"], row["value"]))
        return out

    # ---------------------------------------------------------------- права

    async def set_right(self, user_id: int, right: str, daily_limit: int) -> None:
        await self.conn.execute(
            "INSERT INTO rights (user_id, right, daily_limit) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, right) DO UPDATE SET daily_limit=excluded.daily_limit",
            (user_id, right, daily_limit),
        )
        await self.conn.commit()

    async def drop_right(self, user_id: int, right: str) -> None:
        await self.conn.execute(
            "DELETE FROM rights WHERE user_id=? AND right=?", (user_id, right)
        )
        await self.conn.commit()

    async def drop_all_rights(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM rights WHERE user_id=?", (user_id,))
        await self.conn.commit()

    async def rights_of(self, user_id: int) -> dict[str, int]:
        cur = await self.conn.execute(
            "SELECT right, daily_limit FROM rights WHERE user_id=?", (user_id,)
        )
        return {r["right"]: r["daily_limit"] for r in await cur.fetchall()}

    async def usage_today(self, user_id: int, right: str) -> int:
        day = datetime.now().strftime("%Y-%m-%d")
        cur = await self.conn.execute(
            "SELECT count FROM usage WHERE user_id=? AND right=? AND day=?",
            (user_id, right, day),
        )
        row = await cur.fetchone()
        return row["count"] if row else 0

    async def bump_usage(self, user_id: int, right: str) -> None:
        day = datetime.now().strftime("%Y-%m-%d")
        await self.conn.execute(
            "INSERT INTO usage (user_id, right, day, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(user_id, right, day) DO UPDATE SET count = count + 1",
            (user_id, right, day),
        )
        await self.conn.commit()

    # -------------------------------------------------------------- репорты

    async def add_report(
        self, from_id: int, target_id: int, ref: Optional[int], reason: str
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO reports (from_id, target_id, ref, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (from_id, target_id, ref, reason, int(time.time())),
        )
        await self.conn.commit()
        return cur.lastrowid

    # ----------------------------------------------------------- голосования

    async def add_vote(
        self, target_id: int, author_id: int, reason: str, duration: int, until: int
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO votes (target_id, author_id, reason, duration, until) "
            "VALUES (?, ?, ?, ?, ?)",
            (target_id, author_id, reason, duration, until),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def add_vote_msg(self, vote_id: int, chat_id: int, msg_id: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO vote_msgs VALUES (?, ?, ?)", (vote_id, chat_id, msg_id)
        )
        await self.conn.commit()

    async def vote_msgs(self, vote_id: int) -> list[tuple[int, int]]:
        cur = await self.conn.execute(
            "SELECT chat_id, msg_id FROM vote_msgs WHERE vote_id=?", (vote_id,)
        )
        return [(r["chat_id"], r["msg_id"]) for r in await cur.fetchall()]

    async def cast_ballot(self, vote_id: int, voter: int, value: int) -> None:
        await self.conn.execute(
            "INSERT INTO vote_ballots (vote_id, voter, value) VALUES (?, ?, ?) "
            "ON CONFLICT(vote_id, voter) DO UPDATE SET value=excluded.value",
            (vote_id, voter, value),
        )
        await self.conn.commit()

    async def ballots(self, vote_id: int) -> tuple[int, int]:
        cur = await self.conn.execute(
            "SELECT SUM(value=1) AS yes, SUM(value=0) AS no FROM vote_ballots WHERE vote_id=?",
            (vote_id,),
        )
        row = await cur.fetchone()
        return (row["yes"] or 0), (row["no"] or 0)

    async def get_vote(self, vote_id: int) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM votes WHERE id=?", (vote_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def pending_votes(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM votes WHERE resolved=0 AND until <= ?", (int(time.time()),)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def resolve_vote(self, vote_id: int) -> None:
        await self.conn.execute("UPDATE votes SET resolved=1 WHERE id=?", (vote_id,))
        await self.conn.commit()


db = Database(config.DB_PATH)
