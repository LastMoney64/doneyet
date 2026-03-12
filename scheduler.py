"""
Scheduler jobs – uses python-telegram-bot's built-in JobQueue
(which wraps APScheduler and is already integrated with the asyncio loop)
"""

import asyncio
from datetime import datetime
from telegram.ext import ContextTypes
from telegram.error import TelegramError

import database


# ─── Message formatters ───────────────────────────────────────────────────────

def fmt_deadline(task: dict) -> str:
    if not task.get("deadline"):
        return ""
    try:
        dt = datetime.fromisoformat(str(task["deadline"]))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(task["deadline"])


def _e(text) -> str:
    """HTML 특수문자 이스케이프"""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def task_card_compact(task: dict) -> str:
    """목록용 간단 카드 (HTML) – 제목/마감/상품/ID/링크만"""
    lines = [f"🚀 <b>{_e(task['title'])}</b>"]
    dl = fmt_deadline(task)
    if dl:
        lines.append(f"⏰  마감  |  <code>{dl}</code>")
    if task.get("prizes"):
        lines.append(f"🏆  상품  |  {_e(task['prizes'])}")
    footer = f"🆔 <code>#{task['id']}</code>"
    if task.get("source_url"):
        footer += f"  ·  🔗 {_e(task['source_url'])}"
    lines.append(footer)
    return "\n".join(lines)


def task_card(task: dict, show_how_to_do: bool = False) -> str:
    """
    HTML 포맷 카드
    show_how_to_do=False  → 목록용 (제목/설명/마감/상품/링크)
    show_how_to_do=True   → 상세용 (참여방법 포함)
    """
    lines = [f"🚀 <b>{_e(task['title'])}</b>"]

    dl = fmt_deadline(task)
    if dl:
        lines.append(f"⏰  마감  |  <code>{dl}</code>")

    if task.get("prizes"):
        lines.append(f"🏆  상품  |  {_e(task['prizes'])}")

    if task.get("description"):
        lines.append(f"\n📝 {_e(task['description'])}")

    if show_how_to_do and task.get("how_to_do"):
        lines.append(f"\n📋 <b>참여 방법</b>\n{_e(task['how_to_do'])}")

    footer = f"🆔 <code>#{task['id']}</code>"
    if task.get("source_url"):
        footer += f"  ·  🔗 {_e(task['source_url'])}"
    lines.append("\n" + footer)

    return "\n".join(lines)


# ─── Broadcast helper ─────────────────────────────────────────────────────────

async def broadcast(context: ContextTypes.DEFAULT_TYPE, message: str):
    users = await database.get_all_users()
    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text=message,
                parse_mode="HTML",
            )
            await asyncio.sleep(0.05)
        except TelegramError as e:
            print(f"[broadcast] user {user['user_id']}: {e}")


# ─── Scheduled jobs ───────────────────────────────────────────────────────────

SEP = "─" * 24


async def _build_daily_message(prefix_emoji: str, suffix_msg: str) -> str:
    """아침/저녁 공용 메시지 빌더"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_tasks = await database.get_today_tasks()
    all_tasks = await database.get_all_tasks()

    lines = [f"{prefix_emoji} <b>{today_str} 오늘의 숙제</b>\n"]

    if today_tasks:
        lines.append(f"📅 <b>오늘 마감</b> ({len(today_tasks)}개)\n")
        for t in today_tasks:
            lines.append(SEP)
            lines.append(task_card_compact(t))
        lines.append(SEP)
    else:
        lines.append("오늘 마감인 숙제는 없습니다.")

    upcoming = [t for t in all_tasks if t["id"] not in {x["id"] for x in today_tasks}]
    if upcoming:
        lines.append(f"\n📋 <b>진행중인 숙제</b> {len(upcoming)}개")
        lines.append("전체 숙제 목록은 /tasks 를 입력하세요.")

    lines.append(f"\n{suffix_msg}")
    return "\n".join(lines)


async def job_morning_notification(context: ContextTypes.DEFAULT_TYPE):
    """매일 아침 9시 오늘의 숙제 리스트 전송"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    ntype = f"morning_{today_str}"
    users = await database.get_all_users()
    if not users:
        return

    message = await _build_daily_message("🌅", "💪 오늘도 화이팅!")

    for user in users:
        uid = user["user_id"]
        if await database.already_sent(0, uid, ntype):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=message, parse_mode="HTML")
            await database.mark_sent(0, uid, ntype)
            await asyncio.sleep(0.05)
        except TelegramError as e:
            print(f"[morning] user {uid}: {e}")


async def job_evening_notification(context: ContextTypes.DEFAULT_TYPE):
    """매일 저녁 10시 오늘의 숙제 리스트 전송"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    ntype = f"evening_{today_str}"
    users = await database.get_all_users()
    if not users:
        return

    message = await _build_daily_message("🌙", "✅ 오늘 숙제 잊지 말고 마무리하세요!")

    for user in users:
        uid = user["user_id"]
        if await database.already_sent(0, uid, ntype):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=message, parse_mode="HTML")
            await database.mark_sent(0, uid, ntype)
            await asyncio.sleep(0.05)
        except TelegramError as e:
            print(f"[evening] user {uid}: {e}")


async def job_deadline_reminders(context: ContextTypes.DEFAULT_TYPE):
    """마감 3시간, 2시간, 1시간 전 알림"""
    now = datetime.now()
    users = await database.get_all_users()
    if not users:
        return

    # (알림 라벨, 표시 텍스트, 최소시간, 최대시간)
    windows = [
        ("3h",   "⚠️ 마감 3시간 전 알림!",  2.75,  3.25),
        ("1h",   "🚨 마감 1시간 전 알림!",  0.75,  1.25),
        ("0h",   "🔴 마감 시간입니다!",     -0.25, 0.25),
    ]

    for label, header, low, high in windows:
        tasks = await database.get_tasks_for_deadline_check(low, high)
        for task in tasks:
            try:
                dl = datetime.fromisoformat(str(task["deadline"]))
                mins_left = int((dl - now).total_seconds() / 60)
                time_str = f"약 {mins_left}분 남음" if mins_left > 0 else "마감 시각"
                message = (
                    f"<b>{header}</b>\n\n"
                    f"📌 <b>{_e(task['title'])}</b>\n"
                    f"{'─' * 22}\n"
                    f"⏰ 마감: <code>{dl.strftime('%Y-%m-%d %H:%M')}</code>\n"
                    f"남은 시간: {time_str}\n"
                )
                if task.get("prizes"):
                    message += f"\n🏆 상품: {_e(task['prizes'])}\n"
                message += f"\n🆔 <code>#{task['id']}</code>"
                if task.get("source_url"):
                    message += f"  ·  🔗 {_e(task['source_url'])}"

                for user in users:
                    uid = user["user_id"]
                    if await database.already_sent(task["id"], uid, label):
                        continue
                    try:
                        await context.bot.send_message(
                            chat_id=uid, text=message, parse_mode="Markdown"
                        )
                        await database.mark_sent(task["id"], uid, label)
                        await asyncio.sleep(0.05)
                    except TelegramError as e:
                        print(f"[deadline] user {uid}: {e}")
            except Exception as e:
                print(f"[deadline] task {task.get('id')}: {e}")
