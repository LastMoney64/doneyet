import re
import json
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Optional, Dict
import anthropic
import config

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

ANALYSIS_PROMPT = """아래 내용을 분석해서 숙제/퀘스트/이벤트 정보를 추출해주세요.

내용:
{content}

다음 JSON 형식으로만 응답하세요 (다른 텍스트 절대 포함하지 마세요):
{{
  "is_valid": true,
  "title": "숙제/퀘스트 제목 (간결하게)",
  "description": "무엇을 해야 하는지 설명 (2-3줄)",
  "how_to_do": "참여/제출 방법 단계별 설명 (번호 목록 형식)",
  "deadline": "마감일시 (YYYY-MM-DD HH:MM 형식, 없으면 null, 연도 불명확 시 {current_year} 기준)",
  "prizes": "상품/보상/혜택 내용 (없으면 null)",
  "source_link": "내용 안에 포함된 원문/공식 URL (없으면 null, https://로 시작하는 것만)"
}}

숙제/퀘스트/이벤트 내용이 아닌 경우 is_valid를 false로 설정하세요."""


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://\S+", text.strip()))


def is_twitter_url(url: str) -> bool:
    return bool(re.search(r"(twitter\.com|x\.com)/\w+/status/\d+", url))


def is_telegram_url(url: str) -> bool:
    return bool(re.search(r"t\.me/", url))


async def fetch_url_content(url: str) -> Optional[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                soup = BeautifulSoup(html, "lxml")
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                # Limit to avoid excessive token usage
                return text[:4000]
    except Exception as e:
        print(f"[fetch_url] {url}: {e}")
        return None


async def _call_claude(content: str) -> Optional[Dict]:
    current_year = datetime.now().year
    prompt = ANALYSIS_PROMPT.format(content=content, current_year=current_year)
    try:
        message = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Extract JSON block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"[claude] {e}")
    return None


def parse_deadline(deadline_str: Optional[str]) -> Optional[datetime]:
    if not deadline_str:
        return None
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(deadline_str.strip(), fmt)
        except ValueError:
            continue
    return None


async def analyze_input(text: str) -> Optional[Dict]:
    """URL 또는 텍스트를 받아 숙제 정보 반환.

    반환 형식:
    {
        "is_valid": bool,
        "title": str,
        "description": str,
        "how_to_do": str,
        "deadline": datetime | None,
        "deadline_str": str | None,
        "prizes": str | None,
        "source_url": str,
    }
    """
    source_url = ""
    content = text.strip()

    # 텍스트 길이 제한 (토큰 초과 방지)
    if len(content) > 6000:
        content = content[:6000]

    if is_url(content):
        source_url = content
        # 텔레그램 링크는 크롤링 불가 → 텍스트로 입력 요청
        if is_telegram_url(source_url):
            return {"is_valid": False, "error": "텔레그램 링크는 직접 분석이 어렵습니다.\n\n공지 내용을 텍스트로 복사해서 붙여넣기 해주세요."}
        fetched = await fetch_url_content(content)
        if not fetched:
            return {"is_valid": False, "error": "URL 내용을 가져올 수 없습니다. 텍스트로 직접 붙여넣기 해주세요."}
        content = f"[출처: {source_url}]\n\n{fetched}"

    result = await _call_claude(content)
    if not result:
        return {"is_valid": False, "error": "분석에 실패했습니다. 다시 시도해주세요."}

    # source_url: URL 입력이면 그대로, 텍스트 입력이면 Claude가 추출한 링크 사용
    result["source_url"] = source_url or result.get("source_link") or ""
    deadline_raw = result.get("deadline")
    result["deadline_str"] = deadline_raw
    result["deadline"] = parse_deadline(deadline_raw)

    return result
