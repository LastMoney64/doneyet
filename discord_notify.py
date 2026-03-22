"""
Discord Webhook 알림 헬퍼
- DISCORD_WEBHOOK_URL 환경변수가 설정된 경우에만 동작
- HTML 태그 제거 후 Discord용 텍스트로 변환
"""

import re
import aiohttp
import config


def _html_to_discord(text: str) -> str:
    """HTML 포맷 → Discord 마크다운 변환"""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)  # 나머지 HTML 태그 제거
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


async def send(message: str):
    """Discord 웹훅으로 메시지 전송"""
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        return

    content = _html_to_discord(message)
    if not content:
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"content": content}) as r:
                if r.status not in (200, 204):
                    text = await r.text()
                    print(f"[discord] 전송 실패 {r.status}: {text[:100]}")
    except Exception as e:
        print(f"[discord] 오류: {e}")
