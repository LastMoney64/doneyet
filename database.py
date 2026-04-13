import aiosqlite
import json
import os
import zoneinfo
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import config

DB = config.DATABASE_PATH
_KST = zoneinfo.ZoneInfo("Asia/Seoul")


def _now() -> datetime:
    """KST 기준 현재 시각 (naive datetime – DB 저장값과 동일 기준)"""
    return datetime.now(_KST).replace(tzinfo=None)


async def _load_seed():
    """최초 1회: seed_data.json에서 데이터 복원 (Volume DB가 비어있을 때만)"""
    seed_path = os.path.join(os.path.dirname(__file__), "seed_data.json")
    if not os.path.exists(seed_path):
        return
    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)
    async with aiosqlite.connect(DB) as db:
        # tasks: DB가 비어있을 때만 복원
        row = await db.execute("SELECT COUNT(*) FROM tasks")
        count = (await row.fetchone())[0]
        if count == 0:
            now = _now().isoformat()
            restored = 0
            for t in seed.get("tasks", []):
                if not t.get("is_active", 1):
                    continue
                if t.get("deadline") and str(t["deadline"]) < now:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO tasks (id,title,description,how_to_do,deadline,prizes,source_url,added_by,added_at,is_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t["id"],t["title"],t.get("description"),t.get("how_to_do"),t.get("deadline"),t.get("prizes"),t.get("source_url"),t.get("added_by"),t.get("added_at"),1)
                )
                restored += 1
            print(f"[seed] {restored}개 유효 숙제 복원 완료")
        # admins/users: 항상 머지
        for a in seed.get("admins", []):
            await db.execute(
                "INSERT OR IGNORE INTO admins (user_id,username,added_by,added_at) VALUES (?,?,?,?)",
                (a["user_id"],a.get("username"),a.get("added_by"),a.get("added_at"))
            )
        for u in seed.get("users", []):
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id,username,first_name,chat_type,joined_at,notifications_enabled) VALUES (?,?,?,?,?,?)",
                (u["user_id"],u.get("username"),u.get("first_name"),u.get("chat_type","private"),u.get("joined_at"),u.get("notifications_enabled",1))
            )
        await db.commit()
        print(f"[seed] users {len(seed.get('users',[]))}명 동기화 완료")


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
                notifications_enabled INTEGER DEFAULT 1,
                chat_type             TEXT DEFAULT 'private'
            )
        """)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN chat_type TEXT DEFAULT 'private'")
            await db.commit()
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notifications_sent (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id           INTEGER,
                user_id           INTEGER,
                notification_type TEXT,
                sent_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shoutouts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                text          TEXT NOT NULL,
                schedule_time TEXT NOT NULL,
                active_until  TEXT,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banners (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                text         TEXT NOT NULL,
                active_until TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                text         TEXT NOT NULL,
                active_until TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meetups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                description TEXT,
                location    TEXT,
                address     TEXT,
                event_date  TEXT,
                event_end   TEXT,
                organizer   TEXT,
                prizes      TEXT,
                source_url  TEXT,
                added_by    INTEGER,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active   INTEGER DEFAULT 1
            )
        """)
        await db.commit()
    await _load_seed()


async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def delete_setting(key: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
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
        deadline_str = deadline
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


async def expire_past_tasks() -> int:
    """마감이 지난 숙제를 자동으로 비활성화. 처리된 건수 반환"""
    now = _now().isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE tasks SET is_active = 0 WHERE is_active = 1 AND deadline IS NOT NULL AND deadline < ?",
            (now,),
        )
        await db.commit()
        return cur.rowcount


async def find_duplicate(title: str, source_url: str) -> Optional[Dict]:
    """동일한 source_url 또는 유사한 제목의 숙제가 있으면 반환"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        if source_url:
            async with db.execute(
                "SELECT * FROM tasks WHERE is_active = 1 AND source_url = ?", (source_url,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        import re
        norm = lambda s: re.sub(r"[\s\W]", "", s).lower()
        target = norm(title)
        async with db.execute("SELECT * FROM tasks WHERE is_active = 1") as cur:
            for row in await cur.fetchall():
                if norm(row["title"]) == target:
                    return dict(row)
    return None


async def get_all_tasks() -> List[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE is_active = 1 ORDER BY id ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_today_tasks() -> List[Dict]:
    today = _now().date().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE is_active = 1 AND date(deadline) = ? ORDER BY deadline ASC",
            (today,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_urgent_tasks(within_hours: int = 24) -> List[Dict]:
    now = _now()
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
    now = _now()
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
            """INSERT OR IGNORE INTO users (user_id, username, first_name, chat_type)
               VALUES (?, ?, ?, 'private')""",
            (user_id, username or "", first_name or ""),
        )
        await db.commit()


async def register_chat(chat_id: int, chat_title: str, chat_type: str):
    """그룹/채널을 알림 대상으로 등록"""
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT OR IGNORE INTO users (user_id, username, first_name, chat_type, notifications_enabled)
               VALUES (?, ?, ?, ?, 1)""",
            (chat_id, "", chat_title or "", chat_type),
        )
        await db.commit()


async def unregister_chat(chat_id: int):
    """그룹/채널을 알림 대상에서 제거"""
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (chat_id,))
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


# ─── Shoutout operations ──────────────────────────────────────────────────────

async def add_shoutout(text: str, schedule_time: str, active_until: Optional[datetime]) -> int:
    """샤라웃 추가. schedule_time: 'HH:MM'"""
    until_str = active_until.isoformat() if active_until else None
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "INSERT INTO shoutouts (text, schedule_time, active_until) VALUES (?, ?, ?)",
            (text, schedule_time, until_str),
        )
        await db.commit()
        return cur.lastrowid


async def get_active_shoutouts() -> List[Dict]:
    now = _now().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM shoutouts WHERE active_until IS NULL OR active_until > ? ORDER BY schedule_time",
            (now,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_shoutouts_for_time(hhmm: str) -> List[Dict]:
    """현재 HH:MM에 발송해야 할 활성 샤라웃"""
    now = _now().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM shoutouts WHERE schedule_time = ? AND (active_until IS NULL OR active_until > ?)",
            (hhmm, now),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_shoutout(shoutout_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM shoutouts WHERE id = ?", (shoutout_id,))
        await db.commit()


# ─── Banner operations ────────────────────────────────────────────────────────

async def add_banner(text: str, active_until: Optional[datetime]) -> int:
    """광고 배너 추가"""
    until_str = active_until.isoformat() if active_until else None
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "INSERT INTO banners (text, active_until) VALUES (?, ?)",
            (text, until_str),
        )
        await db.commit()
        return cur.lastrowid


async def get_active_banners() -> List[Dict]:
    now = _now().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM banners WHERE active_until IS NULL OR active_until > ? ORDER BY id",
            (now,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_banner(banner_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM banners WHERE id = ?", (banner_id,))
        await db.commit()


# ─── Pin operations ───────────────────────────────────────────────────────────

async def add_pin(text: str, active_until: Optional[datetime]) -> int:
    """핀 공지 추가"""
    until_str = active_until.isoformat() if active_until else None
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "INSERT INTO pins (text, active_until) VALUES (?, ?)",
            (text, until_str),
        )
        await db.commit()
        return cur.lastrowid


async def get_active_pins() -> List[Dict]:
    now = _now().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pins WHERE active_until IS NULL OR active_until > ? ORDER BY id",
            (now,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_pin(pin_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM pins WHERE id = ?", (pin_id,))
        await db.commit()


# ─── Meetup operations ──────────────────────────────────────────────────────

async def add_meetup(
    title: str,
    description: str,
    location: str,
    address: str,
    event_date: Optional[str],
    event_end: Optional[str],
    organizer: str,
    prizes: str,
    source_url: str,
    added_by: int,
) -> int:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """INSERT INTO meetups (title, description, location, address, event_date, event_end, organizer, prizes, source_url, added_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, description, location, address, event_date, event_end, organizer, prizes, source_url, added_by),
        )
        await db.commit()
        return cur.lastrowid


async def get_all_meetups() -> List[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM meetups WHERE is_active = 1 ORDER BY event_date ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_meetup_by_id(meetup_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM meetups WHERE id = ? AND is_active = 1", (meetup_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_upcoming_meetups(within_days: int = 7) -> List[Dict]:
    now = _now().isoformat()
    until = (_now() + timedelta(days=within_days)).isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM meetups WHERE is_active = 1
               AND event_date IS NOT NULL
               AND event_date >= ?
               AND event_date <= ?
               ORDER BY event_date ASC""",
            (now, until),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_meetups_tomorrow() -> List[Dict]:
    tomorrow = (_now() + timedelta(days=1)).date().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM meetups WHERE is_active = 1 AND date(event_date) = ? ORDER BY event_date ASC",
            (tomorrow,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_meetups_today() -> List[Dict]:
    today = _now().date().isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM meetups WHERE is_active = 1 AND date(event_date) = ? ORDER BY event_date ASC",
            (today,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_meetups_for_reminder(low_hours: float, high_hours: float) -> List[Dict]:
    """event_date가 low_hours ~ high_hours 사이인 활성 밋업"""
    now = _now()
    low = (now + timedelta(hours=low_hours)).isoformat()
    high = (now + timedelta(hours=high_hours)).isoformat()
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM meetups WHERE is_active = 1
               AND event_date IS NOT NULL
               AND event_date >= ?
               AND event_date <= ?""",
            (low, high),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_meetup(meetup_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE meetups SET is_active = 0 WHERE id = ?", (meetup_id,))
        await db.commit()


async def update_meetup_field(meetup_id: int, field: str, value):
    allowed = {"title", "description", "location", "address", "event_date", "event_end", "organizer", "prizes", "source_url"}
    if field not in allowed:
        raise ValueError(f"허용되지 않은 필드: {field}")
    async with aiosqlite.connect(DB) as db:
        await db.execute(f"UPDATE meetups SET {field} = ? WHERE id = ?", (value, meetup_id))
        await db.commit()


async def find_duplicate_meetup(title: str, source_url: str) -> Optional[Dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        if source_url:
            async with db.execute(
                "SELECT * FROM meetups WHERE is_active = 1 AND source_url = ?", (source_url,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        import re
        norm = lambda s: re.sub(r"[\s\W]", "", s).lower()
        target = norm(title)
        async with db.execute("SELECT * FROM meetups WHERE is_active = 1") as cur:
            for row in await cur.fetchall():
                if norm(row["title"]) == target:
                    return dict(row)
    return None


async def expire_past_meetups() -> int:
    """event_date가 지난 밋업을 자동 비활성화"""
    now = _now().isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE meetups SET is_active = 0 WHERE is_active = 1 AND event_date IS NOT NULL AND event_date < ?",
            (now,),
        )
        await db.commit()
        return cur.rowcount
