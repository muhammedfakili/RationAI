#!/usr/bin/env python
"""RationAI quick start (no Docker): seed DB -> index docs -> backend + frontend.

Run: .venv/Scripts/python.exe quickstart.py   (Ctrl+C stops both servers)
"""

import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
HEALTH_URL = "http://localhost:8000/api/v1/health"


def find_python() -> str | None:
    for candidate in (BASE_DIR / ".venv" / "Scripts" / "python.exe", BASE_DIR / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return None


def wait_for_backend(timeout_s: float = 60.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def main() -> int:
    python = find_python()
    if not python:
        print("[HATA] .venv bulunamadı. Kurulum:\n"
              "  python -m venv .venv\n"
              "  .venv/Scripts/pip install -r requirements.txt\n"
              "  (ya da hiç uğraşmadan: docker compose up)")
        return 1

    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        shutil.copy(BASE_DIR / ".env.example", env_file)
        print("[BİLGİ] .env, .env.example'dan oluşturuldu. Sohbet için NVIDIA_API_KEY ya da GROQ_API_KEY ekleyebilirsin (opsiyonel).")

    print("[1/3] Veritabanı hazırlanıyor...")
    subprocess.run([python, "-m", "backend.app.database"], cwd=BASE_DIR, check=True)

    print("[2/3] Bilgi tabanı kontrol ediliyor...")
    subprocess.run([python, "-m", "backend.app.rag", "--if-empty"], cwd=BASE_DIR, check=False)

    print("[3/3] Sunucular başlatılıyor — arayüz: http://localhost:8501 · API: http://localhost:8000/docs")
    backend = subprocess.Popen(
        [python, "-m", "uvicorn", "backend.app.main:app", "--reload", "--port", "8000"], cwd=BASE_DIR
    )
    try:
        if not wait_for_backend():
            print("[UYARI] Backend 60 sn içinde yanıt vermedi; arayüz yine de açılıyor.")
        subprocess.run([python, "-m", "streamlit", "run", "frontend/app.py"], cwd=BASE_DIR)
    except KeyboardInterrupt:
        pass
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
