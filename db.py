"""
db.py — SQLite backend

Schema
------
profile         (user_id, token, name, active, email, filters_json, info_card, device_info_json)
current_account (user_id, token)
blocklists      (user_id, permanent_json, temporary_json, active)
settings        (user_id, explore_url)
country_filters (user_id, enabled, mode, codes_json)

DB file location is controlled by SQLITE_PATH env var (default: meeff.db).
"""

import os
import json
import re
import asyncio
import logging
import tempfile
import shutil
import sqlite3

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("SQLITE_PATH", "meeff.db")

_lock = asyncio.Lock()
_conn: aiosqlite.Connection | None = None


async def _ensure_schema() -> None:
    """Create any missing tables and run column/trigger migrations on the live connection.

    Called on every (re)connect — both at startup and after restore_db — so that an
    older backup file (missing tables or columns added in later versions) is always
    brought up to the current schema before the app uses it.
    """
    await _conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS profile (
            user_id          TEXT NOT NULL,
            token            TEXT NOT NULL,
            name             TEXT,
            active           INTEGER NOT NULL DEFAULT 1,
            email            TEXT,
            filters_json     TEXT,
            info_card        TEXT,
            device_info_json TEXT,
            created_at       TEXT DEFAULT (datetime('now','subsec')),
            PRIMARY KEY (user_id, token)
        );

        CREATE TABLE IF NOT EXISTS current_account (
            user_id TEXT PRIMARY KEY,
            token   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS blocklists (
            user_id        TEXT PRIMARY KEY,
            permanent_json TEXT NOT NULL DEFAULT '[]',
            temporary_json TEXT NOT NULL DEFAULT '[]',
            active         INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            user_id     TEXT PRIMARY KEY,
            explore_url TEXT
        );

        CREATE TABLE IF NOT EXISTS country_filters (
            user_id   TEXT PRIMARY KEY,
            enabled   INTEGER NOT NULL DEFAULT 1,
            mode      TEXT NOT NULL DEFAULT 'exclude',
            codes_json TEXT NOT NULL DEFAULT '[]'
        );
    """)
    await _conn.commit()
    await _migrate_add_created_at()
    await _ensure_created_at_trigger()


async def init_db() -> None:
    """Open the shared connection and create tables. Call once at startup."""
    global _conn
    _conn = await aiosqlite.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = aiosqlite.Row
    await _ensure_schema()
    logger.info("Database initialised at %s", DB_PATH)


async def _migrate_add_created_at() -> None:
    """One-time migration: add created_at to profile if missing, backfill by rowid order.

    SQLite forbids ALTER TABLE ... ADD COLUMN with a non-constant default (like
    datetime('now')) on a table that already has rows, so we add the column plain,
    then backfill in a separate UPDATE.

    Rows created before this migration have no created_at value. We backfill them using
    their rowid (SQLite's insertion-order proxy for a normal, non-WITHOUT-ROWID table) so
    existing accounts keep their relative oldest->newest order.

    A trigger handles all *future* inserts: the column default in CREATE TABLE only
    applies to brand-new databases, not to a column added via ALTER TABLE, so without
    this trigger every account added after migration would get created_at=NULL and
    sort order would break again.
    """
    cur = await _conn.execute("PRAGMA table_info(profile)")
    cols = [r["name"] for r in await cur.fetchall()]
    if "created_at" in cols:
        return
    logger.info("Migrating profile table: adding created_at column")
    await _conn.execute("ALTER TABLE profile ADD COLUMN created_at TEXT")

    await _conn.execute(
        """UPDATE profile
           SET created_at = (
               SELECT datetime('2000-01-01', '+' || sub.rn || ' seconds')
               FROM (
                   SELECT rowid AS rid, ROW_NUMBER() OVER (ORDER BY rowid) AS rn
                   FROM profile
               ) AS sub
               WHERE sub.rid = profile.rowid
           )
           WHERE created_at IS NULL"""
    )
    await _conn.commit()
    await _ensure_created_at_trigger()


async def _ensure_created_at_trigger() -> None:
    """Auto-populate created_at on insert for rows that didn't set it explicitly.

    Needed because a column added via ALTER TABLE doesn't carry the CREATE TABLE
    default, and several INSERT statements across this file don't list created_at.
    """
    await _conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_profile_created_at
           AFTER INSERT ON profile
           WHEN NEW.created_at IS NULL
           BEGIN
               UPDATE profile SET created_at = datetime('now','subsec') WHERE rowid = NEW.rowid;
           END"""
    )
    await _conn.commit()


async def close_db() -> None:
    """Cleanly close the shared connection (call on shutdown)."""
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _get_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("Database not initialised — call await init_db() at startup.")
    return _conn


def _j(v) -> str | None:
    return json.dumps(v) if v is not None else None


def _uj(s) -> object:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _row_to_account(r) -> dict:
    return {
        "token": r["token"],
        "name": r["name"],
        "filters": _uj(r["filters_json"]),
        "active": bool(r["active"]),
        "email": r["email"],
        "created_at": r["created_at"] if "created_at" in r.keys() else None,
    }


async def set_token(user_id, token: str, account_name: str, email: str | None = None, filters=None) -> None:
    async with _lock:
        conn = _get_conn()
        if email:

            await conn.execute(
                "DELETE FROM profile WHERE user_id=? AND email=? AND token!=?",
                (str(user_id), email, token),
            )
        await conn.execute(
            """INSERT INTO profile (user_id, token, name, active, email, filters_json)
               VALUES (?,?,?,1,?,?)
               ON CONFLICT(user_id, token) DO UPDATE SET
                   name=excluded.name,
                   active=1,
                   email=excluded.email,
                   filters_json=COALESCE(excluded.filters_json, profile.filters_json)""",
            (str(user_id), token, account_name, email, _j(filters)),
        )
        await conn.commit()


async def set_account_active(user_id, token: str, active: bool) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "UPDATE profile SET active=? WHERE user_id=? AND token=?",
            (1 if active else 0, str(user_id), token),
        )
        await conn.commit()


async def get_tokens(user_id) -> list[dict]:
    """Return only active accounts, oldest first."""
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT token, name, filters_json, active, email, created_at FROM profile "
        "WHERE user_id=? AND active=1 ORDER BY created_at ASC, rowid ASC",
        (str(user_id),),
    )
    return [_row_to_account(r) for r in await cur.fetchall()]


async def get_all_tokens(user_id) -> list[dict]:
    """Return all accounts regardless of active flag, oldest first."""
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT token, name, filters_json, active, email, created_at FROM profile "
        "WHERE user_id=? ORDER BY created_at ASC, rowid ASC",
        (str(user_id),),
    )
    return [_row_to_account(r) for r in await cur.fetchall()]


async def list_tokens() -> list[dict]:
    """Return all active accounts across all users, oldest first."""
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT user_id, token, name, filters_json, active, email, created_at FROM profile "
        "WHERE active=1 ORDER BY created_at ASC, rowid ASC"
    )
    return [
        {"user_id": r["user_id"], **_row_to_account(r)}
        for r in await cur.fetchall()
    ]


async def set_current_account(user_id, token: str) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT INTO current_account (user_id, token) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token",
            (str(user_id), token),
        )
        await conn.commit()


async def get_current_account(user_id) -> str | None:
    """Return token only if it still exists and is active."""
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT token FROM current_account WHERE user_id=?", (str(user_id),)
    )
    row = await cur.fetchone()
    if not row:
        return None
    token = row["token"]
    cur2 = await conn.execute(
        "SELECT 1 FROM profile WHERE user_id=? AND token=? AND active=1",
        (str(user_id), token),
    )
    return token if await cur2.fetchone() else None


async def delete_token(user_id, token: str) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "DELETE FROM profile WHERE user_id=? AND token=?", (str(user_id), token)
        )
        await conn.commit()


async def set_user_filters(user_id, token: str, filters: dict) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT INTO profile (user_id, token, name, active, filters_json) VALUES (?,?,?,1,?) "
            "ON CONFLICT(user_id, token) DO UPDATE SET filters_json=excluded.filters_json",
            (str(user_id), token, "", _j(filters)),
        )
        await conn.commit()


async def get_user_filters(user_id, token: str) -> dict | None:
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT filters_json FROM profile WHERE user_id=? AND token=?",
        (str(user_id), token),
    )
    row = await cur.fetchone()
    return _uj(row["filters_json"]) if row else None


async def update_token_metadata(user_id, token: str, name: str | None = None,
                                email: str | None = None, active: bool | None = None) -> None:
    parts, vals = [], []
    if name is not None:
        parts.append("name=?"); vals.append(name)
    if email is not None:
        parts.append("email=?"); vals.append(email)
    if active is not None:
        parts.append("active=?"); vals.append(1 if active else 0)
    if not parts:
        return
    vals += [str(user_id), token]
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            f"UPDATE profile SET {', '.join(parts)} WHERE user_id=? AND token=?", vals
        )
        await conn.commit()


async def set_info_card(user_id, token: str, info_card: str, email: str | None = None) -> None:
    async with _lock:
        conn = _get_conn()
        if email:
            await conn.execute(
                "INSERT INTO profile (user_id, token, name, active, email, info_card) VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(user_id, token) DO UPDATE SET info_card=excluded.info_card, email=excluded.email",
                (str(user_id), token, "", email, info_card),
            )
        else:
            await conn.execute(
                "INSERT INTO profile (user_id, token, name, active, info_card) VALUES (?,?,?,1,?) "
                "ON CONFLICT(user_id, token) DO UPDATE SET info_card=excluded.info_card",
                (str(user_id), token, "", info_card),
            )
        await conn.commit()


async def get_info_card(user_id, token: str) -> str | None:
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT info_card FROM profile WHERE user_id=? AND token=?",
        (str(user_id), token),
    )
    row = await cur.fetchone()
    return row["info_card"] if row else None


async def set_device_info(user_id, token: str, device_info: dict) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT INTO profile (user_id, token, name, active, device_info_json) VALUES (?,?,?,1,?) "
            "ON CONFLICT(user_id, token) DO UPDATE SET device_info_json=excluded.device_info_json",
            (str(user_id), token, "", _j(device_info)),
        )
        await conn.commit()


async def get_device_info(user_id, token: str) -> dict | None:
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT device_info_json FROM profile WHERE user_id=? AND token=?",
        (str(user_id), token),
    )
    row = await cur.fetchone()
    return _uj(row["device_info_json"]) if row else None


async def set_explore_url(user_id, url: str) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT INTO settings (user_id, explore_url) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET explore_url=excluded.explore_url",
            (str(user_id), url),
        )
        await conn.commit()


async def get_explore_url(user_id) -> str | None:
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT explore_url FROM settings WHERE user_id=?", (str(user_id),)
    )
    row = await cur.fetchone()
    return row["explore_url"] if row else None


async def get_country_filter(user_id) -> dict:
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT enabled, mode, codes_json FROM country_filters WHERE user_id=?",
        (str(user_id),),
    )
    row = await cur.fetchone()
    if not row:
        return {"user_id": str(user_id), "enabled": True, "mode": "exclude", "codes": []}
    return {
        "user_id": str(user_id),
        "enabled": bool(row["enabled"]),
        "mode": row["mode"],
        "codes": _uj(row["codes_json"]) or [],
    }


async def save_country_filter(user_id, doc: dict) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT INTO country_filters (user_id, enabled, mode, codes_json) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, mode=excluded.mode, codes_json=excluded.codes_json",
            (str(user_id), 1 if doc.get("enabled", True) else 0,
             doc.get("mode", "exclude"), _j(doc.get("codes", []))),
        )
        await conn.commit()


async def get_blocklist_doc(user_id) -> dict | None:
    conn = _get_conn()
    cur = await conn.execute(
        "SELECT permanent_json, temporary_json, active FROM blocklists WHERE user_id=?",
        (str(user_id),),
    )
    row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": str(user_id),
        "permanent": _uj(row["permanent_json"]) or [],
        "temporary": _uj(row["temporary_json"]) or [],
        "active": bool(row["active"]),
    }


async def upsert_blocklist(user_id, *, permanent: list | None = None,
                           temporary: list | None = None, active: bool | None = None) -> None:
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT OR IGNORE INTO blocklists (user_id, permanent_json, temporary_json, active) VALUES (?,?,?,0)",
            (str(user_id), "[]", "[]"),
        )
        if permanent is not None:
            await conn.execute(
                "UPDATE blocklists SET permanent_json=? WHERE user_id=?",
                (_j(permanent), str(user_id)),
            )
        if temporary is not None:
            await conn.execute(
                "UPDATE blocklists SET temporary_json=? WHERE user_id=?",
                (_j(temporary), str(user_id)),
            )
        if active is not None:
            await conn.execute(
                "UPDATE blocklists SET active=? WHERE user_id=?",
                (1 if active else 0, str(user_id)),
            )
        await conn.commit()


async def add_to_blocklist(user_id, entry: str, list_name: str) -> None:
    """Add entry to 'permanent' or 'temporary' blocklist (no-op if already present)."""
    assert list_name in ("permanent", "temporary")
    col = f"{list_name}_json"
    async with _lock:
        conn = _get_conn()
        await conn.execute(
            "INSERT OR IGNORE INTO blocklists (user_id, permanent_json, temporary_json, active) VALUES (?,?,?,0)",
            (str(user_id), "[]", "[]"),
        )
        cur = await conn.execute(
            f"SELECT {col} FROM blocklists WHERE user_id=?", (str(user_id),)
        )
        row = await cur.fetchone()
        current: list = _uj(row[col]) or [] if row else []
        if entry not in current:
            current.append(entry)
            await conn.execute(
                f"UPDATE blocklists SET {col}=? WHERE user_id=?",
                (_j(current), str(user_id)),
            )
        await conn.commit()


async def transfer_user_data(from_user_id, to_user_id) -> None:
    from_id, to_id = str(from_user_id), str(to_user_id)
    async with _lock:
        conn = _get_conn()
        cur = await conn.execute("SELECT * FROM profile WHERE user_id=?", (from_id,))
        for p in await cur.fetchall():
            await conn.execute(
                """INSERT INTO profile (user_id, token, name, active, email, filters_json, info_card, device_info_json)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id, token) DO UPDATE SET
                       name=excluded.name, active=excluded.active,
                       email=excluded.email, filters_json=excluded.filters_json,
                       info_card=excluded.info_card, device_info_json=excluded.device_info_json""",
                (to_id, p["token"], p["name"], p["active"], p["email"],
                 p["filters_json"], p["info_card"], p["device_info_json"]),
            )

        cur2 = await conn.execute("SELECT token FROM current_account WHERE user_id=?", (from_id,))
        ca = await cur2.fetchone()
        if ca:
            await conn.execute(
                "INSERT INTO current_account (user_id, token) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token",
                (to_id, ca["token"]),
            )

        cur3 = await conn.execute("SELECT permanent_json FROM blocklists WHERE user_id=?", (from_id,))
        bl = await cur3.fetchone()
        if bl and bl["permanent_json"]:
            await conn.execute(
                "INSERT INTO blocklists (user_id, permanent_json, temporary_json, active) VALUES (?,?,'[]',0) "
                "ON CONFLICT(user_id) DO UPDATE SET permanent_json=excluded.permanent_json",
                (to_id, bl["permanent_json"]),
            )

        await conn.commit()


async def replace_token(user_id, old_token: str, new_token: str) -> None:
    if not old_token or not new_token or old_token == new_token:
        return
    uid = str(user_id)
    preserve_fields = ["name", "filters_json", "active", "email", "info_card", "device_info_json"]
    try:
        async with _lock:
            conn = _get_conn()
            cur = await conn.execute(
                "SELECT * FROM profile WHERE user_id=? AND token=?", (uid, old_token)
            )
            old_doc = await cur.fetchone()
            cur2 = await conn.execute(
                "SELECT * FROM profile WHERE user_id=? AND token=?", (uid, new_token)
            )
            new_doc = await cur2.fetchone()

            if old_doc:
                if not new_doc:
                    await conn.execute(
                        "UPDATE profile SET token=? WHERE user_id=? AND token=?",
                        (new_token, uid, old_token),
                    )
                else:
                    for f in preserve_fields:
                        if old_doc[f] and not new_doc[f]:
                            await conn.execute(
                                f"UPDATE profile SET {f}=? WHERE user_id=? AND token=?",
                                (old_doc[f], uid, new_token),
                            )
                    await conn.execute(
                        "DELETE FROM profile WHERE user_id=? AND token=?", (uid, old_token)
                    )


            cur3 = await conn.execute(
                "SELECT info_card FROM profile WHERE user_id=? AND token=?", (uid, new_token)
            )
            doc = await cur3.fetchone()
            if doc and doc["info_card"] and old_token in doc["info_card"]:
                content = doc["info_card"]
                try:
                    new_content = re.sub(
                        r"(<b>\s*Token:\s*</b>\s*<code>)(.*?)(</code>)",
                        lambda m: m.group(1) + new_token + m.group(3),
                        content, count=1, flags=re.S | re.I,
                    )
                except Exception:
                    new_content = content.replace(old_token, new_token)
                if new_content != content:
                    await conn.execute(
                        "UPDATE profile SET info_card=? WHERE user_id=? AND token=?",
                        (new_content, uid, new_token),
                    )

            await conn.execute(
                "UPDATE current_account SET token=? WHERE user_id=? AND token=?",
                (new_token, uid, old_token),
            )
            await conn.commit()
    except Exception as e:
        logger.error("replace_token failed: %s", e)


async def backup_db() -> bytes:
    """Return a consistent binary snapshot using SQLite's online backup API."""
    tmp = tempfile.mktemp(suffix=".db")
    try:
        async with _lock:
            conn = _get_conn()
            dst = sqlite3.connect(tmp)
            conn._connection.backup(dst)
            dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def restore_db(data: bytes) -> None:
    """Replace live DB with uploaded bytes, then reconnect."""
    global _conn
    if data[:16] != b"SQLite format 3\x00":
        raise ValueError("Uploaded file is not a valid SQLite database.")
    tmp = tempfile.mktemp(suffix=".db")
    with open(tmp, "wb") as f:
        f.write(data)
    async with _lock:
        if _conn is not None:
            await _conn.close()
            _conn = None
        shutil.move(tmp, DB_PATH)
        _conn = await aiosqlite.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA synchronous=NORMAL")
        await _conn.commit()


        await _ensure_schema()
    logger.info("Database restored successfully.")
