"""
Discord Webhook 알림 헬퍼
- DB settings 테이블의 discord_webhook_url 값을 사용
- HTML 태그 제거 후 Discord용 텍스트로 변환
"""

import re
import aiohttp
import database


def _html_to_discord(text: str) -> str:
    """HTML 포맷 → Discord 마크다운 변환"""
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text.strip()


async def send(message: str) -> bool:
    """Discord 웹훅으로 메시지 전송. 성공 시 True 반환"""
    url = await database.get_setting("discord_webhook_url")
    if not url:
        return False

    content = _html_to_discord(message)
    if not content:
        return False

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"content": content}) as r:
                if r.status in (200, 204):
                    return True
                text = await r.text()
                print(f"[discord] 전송 실패 {r.status}: {text[:100]}")
                return False
    except Exception as e:
        print(f"[discord] 오류: {e}")
        return False
