import aiosqlite
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import config

DB = config.DATABASE_PATH


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                added_by   INTEGER,
                added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT,
                how_to_do   TEXT,
                deadline    TIMESTAMP,
                prizes      TEXT,
                source_url  TEXT,
                added_by    INTEGER,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active   INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id               INTEGER PRIMARY KEY,
                username              TEXT,
                first_name            TEXT,
                joined_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notifications_enabled INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notifications_sent (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id           INTEGER,
                user_id           INTEGER,
                notification_type TEXT,
                sent_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


# ─── Admin operations ────────────────────────────────────────────────────────

async def add_admin(user_id: int, username: str, added_by: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admins (user_id, username, added_by) VALUES (?, ?, ?)",
            (user_id, username, added_by),
        )
        await db.commit()
    return True


async def remove_admin(user_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await db.commit()


async def is_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT user_id FROM admins WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def get_admins() -> List[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM admins ORDER BY added_at") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ─── Task operations ─────────────────────────────────────────────────────────

async def add_task(
    title: str,
    description: str,
    how_to_do: str,
    deadline: Optional[datetime],
    prizes: str,
    source_url: str,
    added_by: int,
) -> int:
    if deadline is None:
        deadline_str = None
    elif isinstance(deadline, str):
        deadline_str = deadline  # 이미 isoformat 문자열
    else:
        deadline_str = deadline.isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """INSERT INTO tasks (title, description, how_to_do, deadline, prizes, source_url, added_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, description, how_to_do, deadline_str, prizes, source_url, added_by),
        )
        await db.commit()
        return cur.lastrowid


async def delete_task(task_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE tasks SET is_active = 0 WHERE id = ?", (task_id,))
        await db.commit()


async def update_task_field(task_id: int, field: str, value):
    """특정 필드 하나만 업데이트"""
    allowed = {"title", "description", "how_to_do", "deadline", "prizes", "source_url"}
    if field not in allowed:
        raise ValueError(f"허용되지 않은 필드: {field}")
    async with aiosqlite.connect(DB) as db:
        await db.execute(f"UPDATE tasks SET {field} = ? WHERE id = ?", (value, task_id))
        await db.commit()


async def get_task_by_id(task_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE id = ? AND is_active = 1", (task_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_tasks() -> List[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM tasks WHERE is_active = 1
               ORDER BY CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline ASC"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_today_tasks() -> List[Dict]:
    today = datetime.now().date().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM tasks WHERE is_active = 1
               AND date(deadline) = ?
               ORDER BY deadline ASC""",
            (today,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_urgent_tasks(within_hours: int = 24) -> List[Dict]:
    now = datetime.now()
    until = (now + timedelta(hours=within_hours)).isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM tasks WHERE is_active = 1
               AND deadline IS NOT NULL
               AND deadline > ?
               AND deadline <= ?
               ORDER BY deadline ASC""",
            (now.isoformat(), until),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_tasks_for_deadline_check(low_hours: float, high_hours: float) -> List[Dict]:
    """마감이 low_hours ~ high_hours 사이인 활성 태스크"""
    now = datetime.now()
    low = (now + timedelta(hours=low_hours)).isoformat()
    high = (now + timedelta(hours=high_hours)).isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM tasks WHERE is_active = 1
               AND deadline IS NOT NULL
               AND deadline >= ?
               AND deadline <= ?""",
            (low, high),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ─── User operations ─────────────────────────────────────────────────────────

async def register_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT OR IGNORE INTO users (user_id, username, first_name)
               VALUES (?, ?, ?)""",
            (user_id, username or "", first_name or ""),
        )
        await db.commit()


async def get_all_users() -> List[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE notifications_enabled = 1"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_notifications(user_id: int, enabled: bool):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE users SET notifications_enabled = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )
        await db.commit()


# ─── Notification dedup ──────────────────────────────────────────────────────

async def already_sent(task_id: int, user_id: int, ntype: str) -> bool:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            """SELECT id FROM notifications_sent
               WHERE task_id = ? AND user_id = ? AND notification_type = ?""",
            (task_id, user_id, ntype),
        ) as cur:
            return await cur.fetchone() is not None


async def mark_sent(task_id: int, user_id: int, ntype: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO notifications_sent (task_id, user_id, notification_type) VALUES (?, ?, ?)",
            (task_id, user_id, ntype),
        )
        await db.commit()
