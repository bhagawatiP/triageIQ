#!/usr/bin/env bash
# ============================================================
#  TriageIQ (Bugs to Quality Coverage) - one-click launcher
#  Run:  ./start.sh   (macOS / Linux / Git Bash)
# ============================================================
cd "$(dirname "$0")/webapp" || exit 1

# Prefer python3, but fall back to python (some Windows setups alias python3 to a stub).
if command -v python3 >/dev/null 2>&1 && python3 -c "print(1)" >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[X] Python 3.9+ not found. Install it from https://www.python.org/downloads/ and re-run ./start.sh"
  exit 1
fi

if [ ! -f config.env ]; then
  cp config.env.example config.env
  echo "[i] Created config.env - open  webapp/config.env , paste your 4 tokens, save, then re-run ./start.sh"
  exit 0
fi

if grep -q "PASTE_" config.env; then
  echo "[X] config.env still has PASTE_ placeholders - fill your 4 tokens in webapp/config.env, then re-run."
  exit 1
fi

echo "[i] Starting TriageIQ... open http://localhost:8756 when it prints the URL."
exec "$PY" triage_server.py
