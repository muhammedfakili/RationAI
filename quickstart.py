#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RationAI Quick Start - Cross-platform Python launcher
Run: python quickstart.py
"""

import os
import sys
import subprocess
import time
from pathlib import Path


def find_python():
    """Find the virtual environment Python executable."""
    base = Path(__file__).parent
    # Windows
    win_python = base / ".venv" / "Scripts" / "python.exe"
    if win_python.exists():
        return str(win_python)
    # Unix/Linux/macOS
    unix_python = base / ".venv" / "bin" / "python"
    if unix_python.exists():
        return str(unix_python)
    return None


def run_cmd(cmd, cwd=None, description="", check=True, capture=False):
    """Run a command and return result."""
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        if capture:
            result = subprocess.run(
                cmd, cwd=cwd, shell=isinstance(cmd, str),
                capture_output=True, text=True, check=check
            )
            if result.stdout:
                print(f"     {result.stdout.strip()}")
            return result
        else:
            subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), check=check)
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] {description or 'Command failed'}: {e}")
        if check:
            raise
    return None


def main():
    base_dir = Path(__file__).parent
    os.chdir(base_dir)

    print("\n" + "=" * 60)
    print("  RATIONAI QUICK START")
    print("=" * 60 + "\n")

    # Find Python
    python = find_python()
    if not python:
        print("[ERROR] Virtual environment not found at .venv/")
        print("  Create it: python -m venv .venv")
        print("  Install deps: .venv/Scripts/pip install -r requirements.txt")
        print("  (Or skip all of this and run: docker compose up)")
        return 1
    print(f"[OK] Using Python: {python}")

    # Check .env
    env_file = base_dir / ".env"
    if not env_file.exists():
        print("[WARNING] .env not found - creating template")
        env_file.write_text("DATABASE_URL=sqlite:///rationai.db\nGROQ_API_KEY=your_groq_api_key_here\n")
        print("  Please edit .env and add your GROQ_API_KEY from https://console.groq.com")
        input("  Press Enter after editing .env...")

    # Step 1: Database setup
    print("\n[1/4] Setting up database and seeding 37 feed ingredients...")
    run_cmd([python, "-m", "backend.app.database"], description="Database setup")

    # Step 2: RAG ingestion
    print("\n[2/4] Ingesting RAG documents into ChromaDB...")
    run_cmd(
        [python, "-m", "backend.app.rag"],  # Run as module to properly import
        description="RAG ingestion", check=False
    )

    # Step 3 & 4: Start servers
    print("\n[3/4] Starting FastAPI backend on http://localhost:8000")
    print("      API docs: http://localhost:8000/docs")

    print("\n[4/4] Starting Streamlit frontend on http://localhost:8501")

    print("\n" + "=" * 60)
    print("  PRESS CTRL+C TO STOP BOTH SERVERS")
    print("=" * 60 + "\n")

    # Start backend
    backend_proc = subprocess.Popen(
        [python, "-m", "uvicorn", "backend.app.main:app", "--reload", "--port", "8000"],
        cwd=base_dir
    )

    # Wait for backend
    time.sleep(3)

    try:
        # Start frontend (blocking)
        frontend_proc = subprocess.Popen(
            [python, "-m", "streamlit", "run", "frontend/app.py"],
            cwd=base_dir
        )
        frontend_proc.wait()
    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        backend_proc.terminate()
        try:
            backend_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend_proc.kill()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())