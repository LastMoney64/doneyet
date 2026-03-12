"""
seed_data.json 변경 감지 시 자동으로 GitHub에 push
main.py와 함께 실행됩니다.
"""
import time
import subprocess
import os
import hashlib

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_data.json")


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def git_push():
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        subprocess.run(["git", "add", "seed_data.json"], cwd=repo_dir, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir, capture_output=True
        )
        if result.returncode == 0:
            return  # 변경 없음
        subprocess.run(
            ["git", "commit", "-m", "auto: update seed_data.json"],
            cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(["git", "push"], cwd=repo_dir, check=True, capture_output=True)
        print("[auto_push] seed_data.json → GitHub push 완료")
    except subprocess.CalledProcessError as e:
        print(f"[auto_push] push 실패: {e}")


def watch():
    print("[auto_push] seed_data.json 감시 시작")
    last_hash = file_hash(SEED_PATH)
    while True:
        time.sleep(10)
        current_hash = file_hash(SEED_PATH)
        if current_hash and current_hash != last_hash:
            last_hash = current_hash
            git_push()


if __name__ == "__main__":
    watch()
