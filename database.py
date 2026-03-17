import aiosqlite
import json
import os
import base64
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import config

DB = config.DATABASE_PATH

_GITHUB_REPO  = "LastMoney64/doneyet"
_GITHUB_FILE  = "seed_data.json"
_GITHUB_API   = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{_GITHUB_FILE}"


async def _push_to_github(content_str: str):
    """seed_data.json을 GitHub API로 직접 업로드 (비동기)"""
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("[seed] GITHUB_TOKEN 없음 – GitHub 업로드 생략")
        return
    try:
        import aiohttp
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
        async with aiohttp.ClientSession() as session:
            # 현재 SHA 조회
            async with session.get(_GITHUB_API, headers=headers) as r:
                sha = (await r.json()).get("sha") if r.status == 200 else None
            payload = {"message": "auto: update seed_data.json", "content": encoded, "branch": "main"}
            if sha:
                payload["sha"] = sha
            async with session.put(_GITHUB_API, headers=headers, json=payload) as r:
                if r.status in (200, 201):
                    print(f"[seed] GitHub 업로드 완료 ✅")
                else:
                    text = await r.text()
                    print(f"[seed] GitHub 업로드 실패 {r.status}: {text[:100]}")
    except Exception as e:
        print(f"[seed] GitHub 업로드 오류: {e}")


async def _export_seed():
    """현재 DB 상태를 seed_data.json에 저장 + GitHub 즉시 업로드
    활성 숙제가 0개면 기존 seed를 보호하기 위해 덮어쓰지 않음"""
    seed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_data.json")
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        tasks  = [dict(r) for r in await (await db.execute("SELECT * FROM tasks")).fetchall()]
        admins = [dict(r) for r in await (await db.execute("SELECT * FROM admins")).fetchall()]
        users  = [dict(r) for r in await (await db.execute("SELECT * FROM users")).fetchall()]

    # 활성 숙제가 없으면 기존 seed 보호
    active_count = sum(1 for t in tasks if t.get("is_active"))
    if active_count == 0:
        print("[seed] 활성 숙제 없음 – seed_data.json 덮어쓰기 건너뜀")
        return

    content_str = json.dumps({"tasks": tasks, "admins": admins, "users": users}, ensure_ascii=False, indent=2, default=str)
    with open(seed_path, "w", encoding="utf-8") as f:
        f.write(content_str)

    # GitHub에 즉시 업로드 (백그라운드 태스크로 실행)
    asyncio.ensure_future(_push_to_github(content_str))


async def _load_seed():
    """seed_data.json에서 데이터 복원 – 항상 users/admins 머지, tasks는 비어있을 때만"""
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
            from datetime import datetime
            now = datetime.now().isoformat()
            restored = 0
            for t in seed.get("tasks", []):
                # 이미 만료됐거나 비활성 숙제는 복원 제외
                if not t.get("is_active", 1):
                    continue
                if t.get("deadline") and str(t["deadline"]) < now:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO tasks (id,title,description,how_to_do,deadline,prizes,source_url,added_by,added_at,is_active) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t["id"],t["title"],t.get("description"),t.get("how_to_do"),t.get("deadline"),t.get("prizes"),t.get("source_url"),t.get("added_by"),t.get("added_at"),1)
                )
                restored += 1
            print(f"[seed] {restored}개 유효 숙제 복원 완료 (만료 제외)")
        # admins/users: 항상 머지 (INSERT OR IGNORE → 기존 데이터 유지, 새 항목만 추가)
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
        # 기존 DB에 chat_type 컬럼이 없으면 추가
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
        await db.commit()
    await _load_seed()


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
        task_id = cur.lastrowid
    await _export_seed()
    return task_id


async def delete_task(task_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE tasks SET is_active = 0 WHERE id = ?", (task_id,))
        await db.commit()
    await _export_seed()


async def update_task_field(task_id: int, field: str, value):
    """특정 필드 하나만 업데이트"""
    allowed = {"title", "description", "how_to_do", "deadline", "prizes", "source_url"}
    if field not in allowed:
        raise ValueError(f"허용되지 않은 필드: {field}")
    async with aiosqlite.connect(DB) as db:
        await db.execute(f"UPDATE tasks SET {field} = ? WHERE id = ?", (value, task_id))
        await db.commit()
    await _export_seed()


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
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "UPDATE tasks SET is_active = 0 WHERE is_active = 1 AND deadline IS NOT NULL AND deadline < ?",
            (now,),
        )
        await db.commit()
        count = cur.rowcount
    if count > 0:
        await _export_seed()
    return count


async def find_duplicate(title: str, source_url: str) -> Optional[Dict]:
    """동일한 source_url 또는 유사한 제목의 숙제가 있으면 반환"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        # source_url 완전 일치 (비어있지 않을 때)
        if source_url:
            async with db.execute(
                "SELECT * FROM tasks WHERE is_active = 1 AND source_url = ?", (source_url,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return dict(row)
        # 제목 정규화 비교 (공백·특수문자 제거 후 비교)
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
            """SELECT * FROM tasks WHERE is_active = 1
               ORDER BY id ASC"""
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
        cur = await db.execute(
            """INSERT OR IGNORE INTO users (user_id, username, first_name, chat_type)
               VALUES (?, ?, ?, 'private')""",
            (user_id, username or "", first_name or ""),
        )
        await db.commit()
        if cur.rowcount > 0:  # 새로 등록된 경우만 백업
            await _export_seed()


async def register_chat(chat_id: int, chat_title: str, chat_type: str):
    """그룹/채널을 알림 대상으로 등록"""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO users (user_id, username, first_name, chat_type, notifications_enabled)
               VALUES (?, ?, ?, ?, 1)""",
            (chat_id, "", chat_title or "", chat_type),
        )
        await db.commit()
        if cur.rowcount > 0:
            await _export_seed()


async def unregister_chat(chat_id: int):
    """그룹/채널을 알림 대상에서 제거"""
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (chat_id,))
        await db.commit()
    await _export_seed()


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
