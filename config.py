import os
from pathlib import Path
from dotenv import load_dotenv

# 스크립트 위치 기준으로 .env 로드 (한글 경로 대응)
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "9"))
MORNING_MINUTE = int(os.getenv("MORNING_MINUTE", "0"))
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "22"))
EVENING_MINUTE = int(os.getenv("EVENING_MINUTE", "0"))
DATABASE_PATH = "donyet.db"
