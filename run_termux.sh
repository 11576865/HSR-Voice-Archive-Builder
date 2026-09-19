#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
pkg install -y python ffmpeg
python -m pip install -r requirements.txt
python -m uvicorn app.server:app --host 127.0.0.1 --port 8765
