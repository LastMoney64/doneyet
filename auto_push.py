"""
seed_data.json 변경 감지 시 GitHub API로 직접 업로드
git 설치 없이 Railway에서도 동작합니다.
"""
import time
import os
import hashlib
import base64
import json

try:
    import requests
except ImportError:
    requests = None

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_data.json")
REPO      = "LastMoney64/doneyet"
FILE_PATH = "seed_data.json"
API_URL   = f"https://api.github.com/repos/{REPO}/contents/{FILE_PATH}"


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def github_push():
    if requests is None:
        print("[auto_push] requests 모듈 없음, 스킵")
        return

    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        print("[auto_push] GITHUB_TOKEN 없음, 스킵")
        return

    try:
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print("[auto_push] seed_data.json 없음, 스킵")
        return

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 현재 파일의 SHA 조회 (업데이트 시 필요)
    r = requests.get(API_URL, headers=headers, timeout=15)
    sha = r.json().get("sha") if r.status_code == 200 else None

    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": "auto: update seed_data.json",
        "content": encoded,
        "branch":  "main",
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(API_URL, headers=headers, json=payload, timeout=15)
    if r.status_code in (200, 201):
        print("[auto_push] seed_data.json → GitHub 업데이트 완료")
    else:
        print(f"[auto_push] 업데이트 실패: {r.status_code} {r.text[:200]}")


def watch():
    print("[auto_push] seed_data.json 감시 시작 (GitHub API 방식)")
    last_hash = file_hash(SEED_PATH)
    while True:
        time.sleep(10)
        current_hash = file_hash(SEED_PATH)
        if current_hash and current_hash != last_hash:
            last_hash = current_hash
            github_push()


if __name__ == "__main__":
    watch()
