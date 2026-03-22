"""
Discord 봇 – 텔레그램과 동일한 명령어 지원
prefix: ! (예: !tasks, !today, !urgent, !task 3)
"""

import asyncio
import discord
from discord.ext import commands
from datetime import datetime
import zoneinfo

import database
import config

KST = zoneinfo.ZoneInfo(config.TIMEZONE)
SEP = "─" * 24


def fmt_deadline(task: dict) -> str:
    if not task.get("deadline"):
        return ""
    try:
        dt = datetime.fromisoformat(str(task["deadline"]))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(task["deadline"])


def task_line(task: dict) -> str:
    dl = fmt_deadline(task)
    line = f"🚀 **{task['title']}**\n"
    if dl:
        line += f"⏰ 마감 | `{dl}`\n"
    if task.get("prizes"):
        line += f"🏆 상품 | {task['prizes']}\n"
    line += f"🆔 `#{task['id']}`"
    if task.get("source_url"):
        line += f"  🔗 {task['source_url']}"
    return line


def task_detail(task: dict) -> str:
    dl = fmt_deadline(task)
    lines = [f"🚀 **{task['title']}**"]
    if dl:
        lines.append(f"⏰ 마감 | `{dl}`")
    if task.get("prizes"):
        lines.append(f"🏆 상품 | {task['prizes']}")
    if task.get("description"):
        lines.append(f"\n📝 {task['description']}")
    if task.get("how_to_do"):
        lines.append(f"\n📋 **참여 방법**\n{task['how_to_do']}")
    footer = f"\n🆔 `#{task['id']}`"
    if task.get("source_url"):
        footer += f"  🔗 {task['source_url']}"
    lines.append(footer)
    return "\n".join(lines)


def make_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    @bot.event
    async def on_ready():
        print(f"[discord_bot] {bot.user} 로그인 완료")

    @bot.command(name="tasks")
    async def cmd_tasks(ctx):
        """전체 숙제 목록"""
        tasks = await database.get_all_tasks()
        if not tasks:
            await ctx.send("📭 현재 진행 중인 숙제가 없습니다.")
            return
        chunks = []
        current = f"📋 **전체 숙제 목록 ({len(tasks)}개)**\n{SEP}\n"
        for t in tasks:
            card = task_line(t) + f"\n{SEP}\n"
            if len(current) + len(card) > 1900:
                chunks.append(current)
                current = card
            else:
                current += card
        chunks.append(current)
        for chunk in chunks:
            await ctx.send(chunk)

    @bot.command(name="today")
    async def cmd_today(ctx):
        """오늘 마감 숙제"""
        tasks = await database.get_today_tasks()
        if not tasks:
            await ctx.send("📭 오늘 마감인 숙제가 없습니다.")
            return
        today = datetime.now(KST).strftime("%Y-%m-%d")
        msg = f"📅 **오늘 마감 숙제 ({today})**\n{SEP}\n"
        for t in tasks:
            msg += task_line(t) + f"\n{SEP}\n"
        await ctx.send(msg)

    @bot.command(name="urgent")
    async def cmd_urgent(ctx):
        """마감 임박 숙제 (24시간 이내)"""
        tasks = await database.get_urgent_tasks(hours=24)
        if not tasks:
            await ctx.send("✅ 24시간 내 마감 임박 숙제가 없습니다.")
            return
        msg = f"🚨 **마감 임박 숙제**\n{SEP}\n"
        for t in tasks:
            msg += task_line(t) + f"\n{SEP}\n"
        await ctx.send(msg)

    @bot.command(name="task")
    async def cmd_task(ctx, task_id: int = None):
        """숙제 상세 정보 (!task 숫자)"""
        if task_id is None:
            await ctx.send("사용법: `!task [숫자]`\n예: `!task 3`")
            return
        task = await database.get_task_by_id(task_id)
        if not task:
            await ctx.send(f"❌ ID {task_id}번 숙제를 찾을 수 없습니다.")
            return
        await ctx.send(task_detail(task))

    @bot.command(name="help")
    async def cmd_help(ctx):
        msg = (
            "📖 **다했니? 봇 명령어**\n"
            f"{SEP}\n"
            "`!tasks` – 전체 숙제 목록\n"
            "`!today` – 오늘 마감 숙제\n"
            "`!urgent` – 마감 임박 숙제 (24시간)\n"
            "`!task [번호]` – 숙제 상세 보기\n"
        )
        await ctx.send(msg)

    return bot


async def run_discord_bot():
    token = config.DISCORD_BOT_TOKEN
    if not token:
        print("[discord_bot] DISCORD_BOT_TOKEN 없음 – Discord 봇 실행 생략")
        return
    bot = make_bot()
    try:
        await bot.start(token)
    except Exception as e:
        print(f"[discord_bot] 실행 오류: {e}")
