"""
다했니? (DoneYet?) – Telegram homework reminder bot
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

import config
import database
import analyzer
import discord_notify
from scheduler import task_card, task_card_compact, job_morning_notification, job_evening_notification, job_deadline_reminders, job_expire_tasks

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── In-memory state ─────────────────────────────────────────────────────────
# pending_tasks[user_id] = analyzed task dict (waiting for admin confirmation)
pending_tasks: dict[int, dict] = {}
# wizard_state[user_id] = {"mode": "add"|"edit", "step": str, "data": dict, "task_id": int}
wizard_state: dict[int, dict] = {}

EDIT_FIELDS = {
    "title":       ("제목",      "새 제목을 입력하세요."),
    "description": ("설명",      "새 설명을 입력하세요."),
    "how_to_do":   ("참여 방법", "새 참여 방법을 입력하세요."),
    "deadline":    ("마감 날짜", "새 마감 날짜를 입력하세요.\n형식: `YYYY-MM-DD HH:MM` (예: 2026-03-15 23:59)"),
    "prizes":      ("상품",      "새 상품 내용을 입력하세요."),
    "source_url":  ("링크",      "새 링크를 입력하세요."),
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def ensure_registered(update: Update):
    u = update.effective_user
    if u:
        await database.register_user(u.id, u.username or "", u.first_name or "")
    # 그룹/채널에서 명령어 사용 시 해당 채팅방도 알림 대상으로 자동 등록
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup", "channel"):
        await database.register_chat(chat.id, chat.title or "", chat.type)


async def is_admin(user_id: int) -> bool:
    if config.SUPER_ADMIN_ID and user_id == config.SUPER_ADMIN_ID:
        return True
    return await database.is_admin(user_id)


def build_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 추가", callback_data=f"confirm_add:{user_id}"),
            InlineKeyboardButton("❌ 취소", callback_data=f"confirm_cancel:{user_id}"),
        ]
    ])


def _esc(text: str) -> str:
    """HTML 특수문자 이스케이프"""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_task_preview(data: dict) -> str:
    lines = [
        "📋 <b>분석 결과</b> – 아래 내용으로 추가할까요?\n",
        f"📌 <b>제목:</b> {_esc(data.get('title', '(없음)'))}",
        f"📝 <b>설명:</b> {_esc(data.get('description', '(없음)'))}",
        f"📋 <b>참여 방법:</b>\n{_esc(data.get('how_to_do', '(없음)'))}",
    ]
    if data.get("deadline_str"):
        lines.append(f"⏰ <b>마감:</b> <code>{_esc(data['deadline_str'])}</code>")
    else:
        lines.append("⏰ <b>마감:</b> 없음")
    if data.get("prizes"):
        lines.append(f"🏆 <b>상품:</b> {_esc(data['prizes'])}")
    if data.get("source_url"):
        lines.append(f"🔗 <b>출처:</b> {_esc(data['source_url'])}")
    return "\n".join(lines)


TASKS_PER_PAGE = 5
SEP = "─" * 24


def tasks_list_message(tasks: list, title: str, compact: bool = False) -> str:
    if not tasks:
        return f"<b>{title}</b>\n\n현재 해당하는 숙제가 없습니다."
    lines = [f"<b>{title}</b> ({len(tasks)}개)\n"]
    for t in tasks:
        lines.append(SEP)
        lines.append(task_card_compact(t) if compact else task_card(t))
    lines.append(SEP)
    return "\n".join(lines)


def tasks_page_message(tasks: list, page: int) -> str:
    """페이지네이션용 메시지 (compact 카드)"""
    total = len(tasks)
    total_pages = max(1, (total + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * TASKS_PER_PAGE
    chunk = tasks[start: start + TASKS_PER_PAGE]

    lines = [f"<b>📚 전체 숙제 목록</b> ({total}개)  <i>{page+1}/{total_pages} 페이지</i>\n"]
    for t in chunk:
        lines.append(SEP)
        lines.append(task_card_compact(t))
    lines.append(SEP)
    return "\n".join(lines)


def build_page_keyboard(tasks: list, page: int) -> Optional[InlineKeyboardMarkup]:
    total = len(tasks)
    total_pages = max(1, (total + TASKS_PER_PAGE - 1) // TASKS_PER_PAGE)
    if total_pages <= 1:
        return None
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀ 이전", callback_data=f"tasks_page:{page-1}"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton("다음 ▶", callback_data=f"tasks_page:{page+1}"))
    return InlineKeyboardMarkup([btns]) if btns else None


# ─── 그룹/채널 참가 인사말 ────────────────────────────────────────────────────────

async def handle_bot_added(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """봇이 그룹/채널에 추가됐을 때 인사말 전송"""
    result: ChatMemberUpdated = update.my_chat_member
    if result is None:
        return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    chat = result.chat

    # 봇이 제거된 경우 → 알림 대상에서 제거
    if new_status in ("left", "kicked"):
        await database.unregister_chat(chat.id)
        log.info(f"채널/그룹 알림 해제: {chat.id} ({chat.title})")
        return

    # 봇이 새로 추가된 경우만 (left/kicked → member/administrator)
    was_out = old_status in ("left", "kicked")
    is_in   = new_status in ("member", "administrator")
    if not (was_out and is_in):
        return

    # 채널/그룹을 알림 대상으로 등록
    await database.register_chat(chat.id, chat.title or "", chat.type)
    log.info(f"채널/그룹 알림 등록: {chat.id} ({chat.title})")

    chat_type_kor = "채널" if chat.type == "channel" else "그룹"

    text = (
        f"👋 안녕하세요! <b>다했니?</b> 봇입니다.\n\n"
        f"📚 이 {chat_type_kor}에 추가되었습니다.\n"
        "숙제/퀘스트 마감을 잊지 않도록 도와드릴게요!\n\n"
        "📌 <b>사용 가능한 명령어</b>\n"
        "/tasks – 전체 숙제 목록\n"
        "/today – 오늘 마감 숙제\n"
        "/urgent – 마감 임박 숙제\n"
        "/task [ID] – 숙제 상세 보기\n"
        "/on – 알림 켜기 · /off – 알림 끄기\n\n"
        "🔔 이 채널에 매일 아침 9시, 저녁 10시 알림이 자동 발송됩니다.\n\n"
        "💡 숙제 등록은 관리자가 <b>봇에게 DM</b>으로 링크나 텍스트를 보내면 됩니다.\n\n"
        "✍️ <i>제작자: 막돈방(라스트머니)</i>"
    )

    try:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.warning(f"그룹 인사말 전송 실패 ({chat.id}): {e}")


# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    u = update.effective_user
    admin = await is_admin(u.id)
    role = "관리자" if admin else "일반 사용자"

    text = (
        f"👋 안녕하세요, <b>{u.first_name}</b>님!\n\n"
        "📚 <b>다했니?</b> 봇에 오신 걸 환영합니다.\n"
        "숙제/퀘스트 마감을 잊지 않도록 도와드립니다.\n\n"
        f"현재 권한: <b>{role}</b>\n\n"
        "📌 사용 가능한 명령어를 보려면 /help 를 입력하세요."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─── /help ────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    u = update.effective_user
    admin = await is_admin(u.id)

    user_cmds = (
        "👤 <b>일반 사용자 명령어</b>\n"
        "/tasks – 전체 숙제 목록\n"
        "/today – 오늘 마감 숙제\n"
        "/urgent – 마감 임박 숙제 (24시간 이내)\n"
        "/task [ID] – 특정 숙제 상세 보기\n"
        "/on – 알림 켜기\n"
        "/off – 알림 끄기\n"
    )

    admin_cmds = (
        "\n🔑 <b>관리자 명령어</b>\n"
        "링크 또는 텍스트 붙여넣기 → 자동 분석 후 추가\n"
        "/addtask – 수동으로 숙제 등록\n"
        "/edittask [ID] – 숙제 정보 수정\n"
        "/deltask [ID] – 숙제 삭제\n"
        "/addadmin [user_id] – 관리자 추가\n"
        "/removeadmin [user_id] – 관리자 제거\n"
        "/admins – 관리자 목록\n"
        "/cancel – 진행 중인 작업 취소\n"
        "\n📢 <b>알림 관리</b>\n"
        "/sendmorning – 아침 알림 즉시 전체 전송\n"
        "/sendevening – 저녁 알림 즉시 전체 전송\n"
        "/notify – 오늘 숙제 수동 전체 전송\n"
        "/broadcast – 자유 공지 전체 채널 배포\n"
        "/addchannel [@채널 또는 ID] – 채널/그룹 알림 등록\n"
        "/listchannels – 등록된 채널/그룹 목록\n"
        "\n🧪 <b>테스트</b>\n"
        "/testmorning – 아침 알림 미리보기\n"
        "/testevening – 저녁 알림 미리보기\n"
        "/testdeadline – 마감 임박 알림 미리보기\n"
    )

    footer = "\n\n👨‍💻 <b>제작자 :</b> 막돈방(라스트머니)"
    text = user_cmds + (admin_cmds if admin else "") + footer
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─── User commands ────────────────────────────────────────────────────────────

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    tasks = await database.get_all_tasks()
    if not tasks:
        await update.message.reply_text("현재 등록된 숙제가 없습니다.")
        return
    msg = tasks_page_message(tasks, 0)
    kbd = build_page_keyboard(tasks, 0)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kbd)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    tasks = await database.get_today_tasks()
    today = datetime.now().strftime("%Y-%m-%d")
    msg = tasks_list_message(tasks, f"📅 오늘({today}) 마감 숙제")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_urgent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    tasks = await database.get_urgent_tasks(24)
    msg = tasks_list_message(tasks, "⚠️ 마감 임박 숙제 (24시간 이내)")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("사용법: /task [숙제 ID]\n예) /task 3")
        return
    task = await database.get_task_by_id(int(args[0]))
    if not task:
        await update.message.reply_text("해당 ID의 숙제를 찾을 수 없습니다.")
        return
    msg = task_card(task, show_how_to_do=True)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    await database.set_notifications(update.effective_user.id, True)
    await update.message.reply_text("🔔 알림이 켜졌습니다!")


async def cmd_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_registered(update)
    await database.set_notifications(update.effective_user.id, False)
    await update.message.reply_text("🔕 알림이 꺼졌습니다.")


# ─── Admin commands ───────────────────────────────────────────────────────────

async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("사용법: /addadmin [user_id]\n예) /addadmin 123456789")
        return
    target_id = int(args[0])
    await database.add_admin(target_id, "", u.id)
    await update.message.reply_text(f"✅ {target_id} 를 관리자로 추가했습니다.")


async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("사용법: /removeadmin [user_id]")
        return
    target_id = int(args[0])
    if target_id == config.SUPER_ADMIN_ID:
        await update.message.reply_text("❌ 최고 관리자는 제거할 수 없습니다.")
        return
    await database.remove_admin(target_id)
    await update.message.reply_text(f"✅ {target_id} 관리자를 제거했습니다.")


async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    admins = await database.get_admins()
    if not admins:
        await update.message.reply_text("등록된 관리자가 없습니다.")
        return
    lines = ["👥 *관리자 목록*\n"]
    for a in admins:
        name = a.get("username") or str(a["user_id"])
        lines.append(f"• `{a['user_id']}` (@{name})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """채널/그룹을 알림 대상으로 수동 등록. 사용법: /addchannel @채널명 또는 /addchannel -100xxxxxxxxx"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "사용법: /addchannel @채널유저명 또는 /addchannel -100xxxxxxxxx\n\n"
            "채널 ID를 모르면 채널에서 아무 메시지나 봇에게 포워드해주세요."
        )
        return
    target = args[0]
    try:
        chat = await ctx.bot.get_chat(target)
        await database.register_chat(chat.id, chat.title or target, chat.type)
        await update.message.reply_text(
            f"✅ 채널/그룹 등록 완료!\n"
            f"📌 이름: <b>{chat.title}</b>\n"
            f"🆔 ID: <code>{chat.id}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ 등록 실패: {e}\n\n"
            "봇이 해당 채널의 관리자인지 확인해주세요."
        )


async def cmd_listchannels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """등록된 채널/그룹 목록 보기"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    users = await database.get_all_users()
    channels = [x for x in users if x.get("chat_type") in ("channel", "group", "supergroup")]
    privates = [x for x in users if x.get("chat_type") == "private" or not x.get("chat_type")]
    lines = [f"📋 <b>알림 대상 현황</b>\n",
             f"👤 개인 유저: {len(privates)}명",
             f"📢 채널/그룹: {len(channels)}개"]

    if channels:
        lines.append("\n<b>── 채널/그룹 ──</b>")
        for c in channels:
            name = c.get("first_name") or "(이름없음)"
            lines.append(f"• {name}  <code>{c['user_id']}</code>")

    if privates:
        lines.append("\n<b>── 개인 유저 ──</b>")
        for p in privates:
            username = f"@{p['username']}" if p.get("username") else ""
            name = p.get("first_name") or "(이름없음)"
            lines.append(f"• {name} {username}  <code>{p['user_id']}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_setdiscord(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """디스코드 웹훅 URL 등록"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    if not ctx.args:
        current = await database.get_setting("discord_webhook_url")
        if current:
            await update.message.reply_text("✅ 디스코드 웹훅이 등록되어 있습니다.\n삭제하려면 /deldiscord")
        else:
            await update.message.reply_text("❌ 등록된 디스코드 웹훅이 없습니다.\n\n사용법: /setdiscord [웹훅URL]")
        return
    webhook_url = ctx.args[0]
    if not webhook_url.startswith("https://discord.com/api/webhooks/"):
        await update.message.reply_text("❌ 올바른 디스코드 웹훅 URL이 아닙니다.\nhttps://discord.com/api/webhooks/... 형식이어야 합니다.")
        return
    await database.set_setting("discord_webhook_url", webhook_url)
    await discord_notify.send("✅ 다했니? 봇 디스코드 알림이 연결되었습니다!")
    await update.message.reply_text("✅ 디스코드 웹훅이 등록되었습니다!\n테스트 메시지를 디스코드로 전송했습니다.")


async def cmd_deldiscord(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """디스코드 웹훅 URL 삭제"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    await database.delete_setting("discord_webhook_url")
    await update.message.reply_text("✅ 디스코드 웹훅이 삭제되었습니다.")


async def cmd_deltask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("사용법: /deltask [숙제 ID]\n예) /deltask 3")
        return
    task_id = int(args[0])
    task = await database.get_task_by_id(task_id)
    if not task:
        await update.message.reply_text("해당 ID의 숙제를 찾을 수 없습니다.")
        return
    await database.delete_task(task_id)
    await update.message.reply_text(f"✅ 숙제 <b>{task['title']}</b> (ID: {task_id}) 를 삭제했습니다.", parse_mode=ParseMode.HTML)


async def cmd_notify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """관리자가 수동으로 오늘 숙제 알림을 전체 전송"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    tasks = await database.get_today_tasks()
    if not tasks:
        await update.message.reply_text("오늘 마감인 숙제가 없습니다.")
        return
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📢 *오늘의 숙제 공지* ({today})\n"]
    for t in tasks:
        lines.append(task_card(t))
        lines.append("")
    lines.append("💪 화이팅!")
    message = "\n".join(lines)
    users = await database.get_all_users()
    sent = 0
    for user in users:
        try:
            await ctx.bot.send_message(
                chat_id=user["user_id"], text=message, parse_mode=ParseMode.HTML
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ {sent}명에게 알림을 보냈습니다.")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """관리자가 자유 텍스트를 전체 사용자/채널에 브로드캐스트"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return

    # 명령어 뒤 텍스트를 원문 그대로 추출 (줄바꿈 보존)
    raw = update.message.text or ""
    # "/broadcast" 또는 "/broadcast@botname" 부분만 제거
    text = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""

    if not text:
        # wizard 방식으로 메시지 입력 받기
        wizard_state[u.id] = {"mode": "broadcast"}
        await update.message.reply_text(
            "📢 *전체 공지 모드*\n\n전송할 메시지를 입력해주세요.\n취소하려면 /cancel 입력",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await _do_broadcast(update, ctx, text)


async def _do_broadcast(update, ctx, text: str):
    users = await database.get_all_users()
    sent, fail = 0, 0
    for user in users:
        try:
            await ctx.bot.send_message(chat_id=user["user_id"], text=text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await update.message.reply_text(f"✅ 전송 완료!\n📨 성공: {sent}개  ❌ 실패: {fail}개")


async def cmd_send_evening(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """저녁 알림을 지금 즉시 전체 전송"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return

    from scheduler import _build_daily_message
    message = await _build_daily_message("🌙", "✅ 오늘 숙제 잊지 말고 마무리하세요!")
    users = await database.get_all_users()
    sent, fail = 0, 0
    for user in users:
        try:
            await ctx.bot.send_message(
                chat_id=user["user_id"], text=message, parse_mode=ParseMode.HTML
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            fail += 1
            log.warning(f"[sendevening] {user['user_id']} 실패: {e}")
    await update.message.reply_text(f"✅ 저녁 알림 전송 완료\n📤 성공: {sent}개  ❌ 실패: {fail}개")


async def cmd_send_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """아침 알림을 지금 즉시 전체 전송"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return

    from scheduler import _build_daily_message
    message = await _build_daily_message("🌅", "💪 오늘도 화이팅!")
    users = await database.get_all_users()
    sent, fail = 0, 0
    for user in users:
        try:
            await ctx.bot.send_message(
                chat_id=user["user_id"], text=message, parse_mode=ParseMode.HTML
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            fail += 1
            log.warning(f"[sendmorning] {user['user_id']} 실패: {e}")
    await update.message.reply_text(f"✅ 아침 알림 전송 완료\n📤 성공: {sent}개  ❌ 실패: {fail}개")


# ─── Admin: test alarm commands ──────────────────────────────────────────────

async def cmd_test_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """아침 알림 미리보기 (본인에게만 전송)"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    from scheduler import _build_daily_message
    msg = await _build_daily_message("🌅", "💪 오늘도 화이팅!")
    msg = "[테스트]\n" + msg
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_test_evening(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """저녁 알림 미리보기 (본인에게만 전송)"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    from scheduler import _build_daily_message
    msg = await _build_daily_message("🌙", "✅ 오늘 숙제 잊지 말고 마무리하세요!")
    msg = "[테스트]\n" + msg
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_test_deadline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """마감 임박 알림 미리보기 – 가장 가까운 숙제로 시뮬레이션 (본인에게만 전송)"""
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return

    # 마감이 있는 활성 숙제 중 가장 가까운 것 사용
    all_tasks = await database.get_all_tasks()
    tasks_with_dl = [t for t in all_tasks if t.get("deadline")]
    if not tasks_with_dl:
        await update.message.reply_text("마감일이 설정된 숙제가 없습니다.")
        return

    task = tasks_with_dl[0]  # 가장 마감이 가까운 것
    dl = datetime.fromisoformat(str(task["deadline"]))
    now = datetime.now()
    mins_left = int((dl - now).total_seconds() / 60)
    time_desc = f"약 {mins_left}분" if mins_left > 0 else "이미 마감됨"

    for hours_label in ["3시간", "2시간", "1시간"]:
        message = (
            f"⚠️ *[테스트] 마감 {hours_label} 전 알림!*\n\n"
            f"🚀 *{task['title']}*\n"
            f"{'─' * 22}\n"
            f"⏰ 마감: `{dl.strftime('%Y-%m-%d %H:%M')}`\n"
            f"실제 남은 시간: {time_desc}\n"
        )
        if task.get("description"):
            message += f"\n📝 {task['description']}\n"
        if task.get("prizes"):
            message += f"\n🏆 상품: {task['prizes']}\n"
        message += f"\n🆔 `#{task['id']}`"
        if task.get("source_url"):
            message += f"  ·  🔗 {task['source_url']}"

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)
        await asyncio.sleep(0.5)

    await update.message.reply_text("✅ 마감 임박 알림 테스트 완료! (3시간 / 2시간 / 1시간 전 메시지)")


# ─── /cancel – wizard 중단 ────────────────────────────────────────────────────

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id in wizard_state:
        wizard_state.pop(u.id)
        await update.message.reply_text("❌ 작업이 취소되었습니다.")
    else:
        await update.message.reply_text("취소할 작업이 없습니다.")


# ─── Admin: text/URL analysis flow ───────────────────────────────────────────

# ─── /addtask – 수동 등록 ─────────────────────────────────────────────────────

ADD_STEPS = ["title", "description", "how_to_do", "deadline", "prizes", "source_url"]
ADD_PROMPTS = {
    "title":       "📌 <b>제목</b>을 입력하세요.",
    "description": "📝 <b>설명</b>을 입력하세요.\n(없으면 <code>-</code> 입력)",
    "how_to_do":   "📋 <b>참여 방법</b>을 입력하세요.\n(없으면 <code>-</code> 입력)",
    "deadline":    "⏰ <b>마감 날짜</b>를 입력하세요.\n형식: <code>YYYY-MM-DD HH:MM</code> (예: 2026-03-15 23:59)\n(없으면 <code>-</code> 입력)",
    "prizes":      "🏆 <b>상품</b>을 입력하세요.\n(없으면 <code>-</code> 입력)",
    "source_url":  "🔗 <b>출처 링크</b>를 입력하세요.\n(없으면 <code>-</code> 입력)",
}


async def cmd_addtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    wizard_state[u.id] = {"mode": "add", "step": "title", "data": {}}
    await update.message.reply_text(
        "✍️ <b>수동 숙제 등록을 시작합니다.</b>\n\n" + ADD_PROMPTS["title"],
        parse_mode=ParseMode.HTML,
    )


# ─── /edittask – 숙제 수정 ────────────────────────────────────────────────────

def build_edit_keyboard(task_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"📌 제목",      callback_data=f"editfield:{task_id}:title"),
         InlineKeyboardButton(f"📝 설명",      callback_data=f"editfield:{task_id}:description")],
        [InlineKeyboardButton(f"📋 참여 방법", callback_data=f"editfield:{task_id}:how_to_do"),
         InlineKeyboardButton(f"⏰ 마감 날짜", callback_data=f"editfield:{task_id}:deadline")],
        [InlineKeyboardButton(f"🏆 상품",      callback_data=f"editfield:{task_id}:prizes"),
         InlineKeyboardButton(f"🔗 링크",      callback_data=f"editfield:{task_id}:source_url")],
        [InlineKeyboardButton(f"❌ 취소",      callback_data=f"editfield:{task_id}:cancel")],
    ]
    return InlineKeyboardMarkup(buttons)


async def cmd_edittask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_admin(u.id):
        await update.message.reply_text("❌ 관리자만 사용할 수 있습니다.")
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("사용법: `/edittask [ID]`\n예) `/edittask 3`", parse_mode=ParseMode.HTML)
        return
    task_id = int(args[0])
    task = await database.get_task_by_id(task_id)
    if not task:
        await update.message.reply_text(f"❌ ID `{task_id}` 숙제를 찾을 수 없습니다.", parse_mode=ParseMode.HTML)
        return
    from scheduler import fmt_deadline
    dl = fmt_deadline(task)
    preview = (
        f"✏️ *숙제 수정 – #{task_id}*\n\n"
        f"📌 제목: {task['title']}\n"
        f"📝 설명: {task.get('description') or '없음'}\n"
        f"📋 참여 방법: {'있음' if task.get('how_to_do') else '없음'}\n"
        f"⏰ 마감: {dl or '없음'}\n"
        f"🏆 상품: {task.get('prizes') or '없음'}\n"
        f"🔗 링크: {task.get('source_url') or '없음'}\n\n"
        "수정할 항목을 선택하세요:"
    )
    await update.message.reply_text(preview, parse_mode=ParseMode.HTML, reply_markup=build_edit_keyboard(task_id))


async def _handle_add_wizard(update: Update, u, text: str):
    """수동 등록 단계별 처리"""
    state = wizard_state[u.id]
    step = state["step"]
    data = state["data"]

    value = "" if text.strip() == "-" else text.strip()
    data[step] = value

    idx = ADD_STEPS.index(step)
    if idx + 1 < len(ADD_STEPS):
        next_step = ADD_STEPS[idx + 1]
        state["step"] = next_step
        try:
            await update.message.reply_text(ADD_PROMPTS[next_step], parse_mode=ParseMode.HTML)
        except Exception as e:
            log.error(f"[wizard] 프롬프트 전송 실패 step={next_step}: {e}")
            await update.message.reply_text(f"⚠️ 오류: {e}\n다시 입력해 주세요.")
    else:
        # 모든 필드 입력 완료 → 미리보기 + 확인
        wizard_state.pop(u.id)
        # deadline 파싱
        dl_raw = data.get("deadline", "")
        dl_parsed = None
        if dl_raw:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dl_parsed = datetime.strptime(dl_raw, fmt)
                    break
                except ValueError:
                    pass
        preview_data = {
            "title":       data.get("title", ""),
            "description": data.get("description", ""),
            "how_to_do":   data.get("how_to_do", ""),
            "deadline_str": dl_raw,
            "deadline":    dl_parsed.isoformat() if dl_parsed else None,
            "prizes":      data.get("prizes", ""),
            "source_url":  data.get("source_url", ""),
            "is_valid":    True,
        }
        pending_tasks[u.id] = preview_data
        try:
            await update.message.reply_text(
                fmt_task_preview(preview_data),
                parse_mode=ParseMode.HTML,
                reply_markup=build_confirm_keyboard(u.id),
            )
        except Exception as e:
            log.error(f"[addtask wizard] preview send failed: {e}")
            await update.message.reply_text(
                f"⚠️ 미리보기 전송 중 오류가 발생했습니다: {e}\n\n/addtask 로 다시 시도해 주세요."
            )


async def _handle_edit_wizard(update: Update, u, text: str):
    """수정 단계 처리 – 사용자가 새 값 입력"""
    state = wizard_state[u.id]
    task_id = state["task_id"]
    field = state["field"]
    wizard_state.pop(u.id)

    value = None if text.strip() == "-" else text.strip()

    # deadline은 파싱 필요
    if field == "deadline" and value:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                pass
        if not parsed:
            await update.message.reply_text("❌ 날짜 형식이 올바르지 않습니다.\n예: `2026-03-15 23:59`", parse_mode=ParseMode.HTML)
            return
        value = parsed.isoformat()

    await database.update_task_field(task_id, field, value)
    label = EDIT_FIELDS[field][0]
    await update.message.reply_text(
        f"✅ *#{task_id}* 의 *{label}* 이(가) 수정되었습니다.",
        parse_mode=ParseMode.HTML,
    )


async def handle_forwarded_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """채널 메시지를 포워드하면 채널 ID를 알려주고 등록 제안"""
    u = update.effective_user
    if not await is_admin(u.id):
        return

    msg = update.message
    chat_id, chat_title, chat_type = None, None, None

    # 채널에서 포워드된 경우
    if msg.forward_origin and hasattr(msg.forward_origin, "chat"):
        origin_chat = msg.forward_origin.chat
        chat_id = origin_chat.id
        chat_title = origin_chat.title or "(이름없음)"
        chat_type = origin_chat.type
    elif msg.forward_from_chat:
        chat_id = msg.forward_from_chat.id
        chat_title = msg.forward_from_chat.title or "(이름없음)"
        chat_type = msg.forward_from_chat.type

    if not chat_id:
        return

    # 이미 등록 여부 확인
    all_users = await database.get_all_users()
    already = any(x["user_id"] == chat_id for x in all_users)

    if already:
        await msg.reply_text(
            f"✅ 이미 알림 대상으로 등록된 채널입니다.\n"
            f"📌 <b>{chat_title}</b>  (<code>{chat_id}</code>)",
            parse_mode=ParseMode.HTML
        )
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 등록", callback_data=f"regchat:{chat_id}:{chat_type}"),
        InlineKeyboardButton("❌ 취소", callback_data="regchat:cancel"),
    ]])
    await msg.reply_text(
        f"📢 포워드된 채널 감지!\n\n"
        f"📌 이름: <b>{chat_title}</b>\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"유형: {chat_type}\n\n"
        f"이 채널을 알림 대상으로 등록할까요?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )
    # 채널 제목 임시 저장
    ctx.bot_data[f"regchat_{chat_id}"] = chat_title


async def callback_regchat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """채널 등록 콜백"""
    query = update.callback_query
    await query.answer()
    data = query.data  # "regchat:{chat_id}:{chat_type}" or "regchat:cancel"

    if data == "regchat:cancel":
        await query.edit_message_text("❌ 등록을 취소했습니다.")
        return

    parts = data.split(":")
    chat_id = int(parts[1])
    chat_type = parts[2]
    chat_title = ctx.bot_data.pop(f"regchat_{chat_id}", "(이름없음)")

    await database.register_chat(chat_id, chat_title, chat_type)
    await query.edit_message_text(
        f"✅ 채널 등록 완료!\n"
        f"📌 <b>{chat_title}</b>  (<code>{chat_id}</code>)\n\n"
        f"이제 매일 아침/저녁 알림이 해당 채널로 전송됩니다.",
        parse_mode=ParseMode.HTML
    )


async def handle_admin_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """관리자가 링크 또는 텍스트를 보내면 Claude로 분석 후 확인 요청"""
    u = update.effective_user
    if not await is_admin(u.id):
        return

    await ensure_registered(update)

    text = (update.message.text or "").strip()
    if not text:
        return

    # wizard 진행 중이면 wizard가 처리
    if u.id in wizard_state:
        state = wizard_state[u.id]
        if state["mode"] == "add":
            await _handle_add_wizard(update, u, text)
        elif state["mode"] == "edit":
            await _handle_edit_wizard(update, u, text)
        elif state["mode"] == "broadcast":
            wizard_state.pop(u.id)
            await _do_broadcast(update, ctx, text)
        return

    analyzing_msg = await update.message.reply_text("🔍 내용을 분석하는 중입니다... 잠시만 기다려주세요.")

    try:
        result = await analyzer.analyze_input(text)
    except Exception as e:
        log.error(f"[analyze] API 오류: {e}")
        await analyzing_msg.edit_text(f"❌ 분석 중 오류가 발생했습니다.\n<code>{e}</code>", parse_mode=ParseMode.HTML)
        return

    if not result or not result.get("is_valid"):
        err = result.get("error", "숙제/퀘스트 내용을 찾을 수 없습니다.") if result else "분석 실패"
        await analyzing_msg.edit_text(f"❌ {err}\n\n숙제/퀘스트 내용이 포함된 링크나 텍스트를 보내주세요.")
        return

    pending_tasks[u.id] = result
    preview = fmt_task_preview(result)
    try:
        await analyzing_msg.delete()
        await update.message.reply_text(
            preview,
            parse_mode=ParseMode.HTML,
            reply_markup=build_confirm_keyboard(u.id),
        )
    except Exception as e:
        log.error(f"[analyze preview] send failed: {e}")
        await update.message.reply_text(
            f"⚠️ 미리보기 전송 중 오류가 발생했습니다.\n<code>{e}</code>",
            parse_mode=ParseMode.HTML
        )


# ─── Callback handlers ────────────────────────────────────────────────────────

async def callback_tasks_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """tasks_page:{page} 콜백 – 페이지 전환"""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    tasks = await database.get_all_tasks()
    msg = tasks_page_message(tasks, page)
    kbd = build_page_keyboard(tasks, page)
    await query.edit_message_text(msg, parse_mode=ParseMode.HTML, reply_markup=kbd)

async def callback_editfield(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """editfield:{task_id}:{field} 콜백 처리"""
    query = update.callback_query
    await query.answer()
    _, task_id_str, field = query.data.split(":", 2)

    if field == "cancel":
        wizard_state.pop(query.from_user.id, None)
        await query.edit_message_text("❌ 수정이 취소되었습니다.")
        return

    task_id = int(task_id_str)
    if not await is_admin(query.from_user.id):
        await query.answer("관리자만 수정할 수 있습니다.", show_alert=True)
        return

    label, prompt = EDIT_FIELDS[field]
    wizard_state[query.from_user.id] = {"mode": "edit", "task_id": task_id, "field": field}
    await query.edit_message_text(
        f"✏️ *#{task_id} – {label} 수정*\n\n{prompt}\n\n(취소하려면 `/cancel` 입력)",
        parse_mode=ParseMode.HTML,
    )


async def callback_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # "confirm_add:{user_id}" or "confirm_cancel:{user_id}"
    action, uid_str = data.split(":", 1)
    uid = int(uid_str)

    # Only the same admin can confirm
    if query.from_user.id != uid:
        await query.answer("이 버튼은 본인만 누를 수 있습니다.", show_alert=True)
        return

    if action == "confirm_cancel":
        pending_tasks.pop(uid, None)
        await query.edit_message_text("❌ 취소되었습니다.")
        return

    # confirm_add
    task_data = pending_tasks.pop(uid, None)
    if not task_data:
        await query.edit_message_text("⚠️ 이미 처리된 요청입니다.")
        return

    # 중복 체크
    dup = await database.find_duplicate(
        title=task_data.get("title", ""),
        source_url=task_data.get("source_url", ""),
    )
    if dup:
        await query.edit_message_text(
            f"⚠️ <b>중복 숙제 감지!</b>\n\n"
            f"이미 동일한 숙제가 등록되어 있습니다.\n"
            f"📌 <b>{dup['title']}</b>\n"
            f"🆔 ID: <code>#{dup['id']}</code>\n\n"
            f"추가를 취소했습니다.",
            parse_mode=ParseMode.HTML,
        )
        return

    task_id = await database.add_task(
        title=task_data.get("title", "제목 없음"),
        description=task_data.get("description", ""),
        how_to_do=task_data.get("how_to_do", ""),
        deadline=task_data.get("deadline"),
        prizes=task_data.get("prizes") or "",
        source_url=task_data.get("source_url", ""),
        added_by=uid,
    )
    await query.edit_message_text(
        f"✅ 숙제가 추가되었습니다!\n"
        f"📌 <b>{task_data.get('title')}</b>\n"
        f"🆔 ID: <code>{task_id}</code>",
        parse_mode=ParseMode.HTML,
    )

    # Discord 알림
    task_full = await database.get_task_by_id(task_id)
    if task_full:
        from scheduler import task_card_compact
        await discord_notify.send(f"✅ 새 숙제가 추가되었습니다!\n\n{task_card_compact(task_full)}")


# ─── Application setup ────────────────────────────────────────────────────────

async def post_init(app: Application):
    """봇 시작 시 DB 초기화 및 super-admin 등록"""
    await database.init_db()
    if config.SUPER_ADMIN_ID:
        await database.add_admin(config.SUPER_ADMIN_ID, "super_admin", config.SUPER_ADMIN_ID)
        log.info(f"Super admin registered: {config.SUPER_ADMIN_ID}")


def main():
    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
    if not config.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # User commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("urgent", cmd_urgent))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))

    # Admin commands
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("listchannels", cmd_listchannels))
    app.add_handler(CommandHandler("setdiscord", cmd_setdiscord))
    app.add_handler(CommandHandler("deldiscord", cmd_deldiscord))
    app.add_handler(CommandHandler("deltask", cmd_deltask))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("sendmorning", cmd_send_morning))
    app.add_handler(CommandHandler("sendevening", cmd_send_evening))
    app.add_handler(CommandHandler("testmorning", cmd_test_morning))
    app.add_handler(CommandHandler("testevening", cmd_test_evening))
    app.add_handler(CommandHandler("testdeadline", cmd_test_deadline))
    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("edittask", cmd_edittask))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Admin: 포워드된 메시지 → 채널 등록 (포워드 메시지만 처리)
    app.add_handler(
        MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forwarded_channel)
    )
    # Admin: 일반 텍스트/링크 → Claude 분석 (포워드 제외)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.FORWARDED & filters.ChatType.PRIVATE, handle_admin_message)
    )

    # 봇이 그룹/채널에 추가됐을 때 인사말
    app.add_handler(ChatMemberHandler(handle_bot_added, ChatMemberHandler.MY_CHAT_MEMBER))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(callback_tasks_page, pattern=r"^tasks_page:\d+$"))
    app.add_handler(CallbackQueryHandler(callback_editfield, pattern=r"^editfield:\d+:\w+$"))
    app.add_handler(CallbackQueryHandler(callback_confirm, pattern=r"^confirm_(add|cancel):\d+$"))
    app.add_handler(CallbackQueryHandler(callback_regchat, pattern=r"^regchat:"))

    # JobQueue 스케줄러 (봇 내장 – asyncio 이벤트 루프와 통합됨)
    import datetime as dt
    jq = app.job_queue
    import zoneinfo
    tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    # 매일 아침 9시 알림
    morning_time = dt.time(config.MORNING_HOUR, config.MORNING_MINUTE, tzinfo=tz)
    jq.run_daily(job_morning_notification, time=morning_time, name="morning_notification")
    # 매일 저녁 10시 알림
    evening_time = dt.time(config.EVENING_HOUR, config.EVENING_MINUTE, tzinfo=tz)
    jq.run_daily(job_evening_notification, time=evening_time, name="evening_notification")
    # 마감 임박 알림 – 15분마다
    jq.run_repeating(job_deadline_reminders, interval=900, first=60, name="deadline_reminders")
    # 만료 숙제 자동 정리 – 1시간마다
    jq.run_repeating(job_expire_tasks, interval=3600, first=30, name="expire_tasks")

    log.info("Bot is running... (JobQueue started)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
