#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
command -v python >/dev/null 2>&1 || pkg install -y python
command -v ffmpeg >/dev/null 2>&1 || pkg install -y ffmpeg
python -m app.preflight --deps-only >/dev/null 2>&1 || python -m pip install -r requirements.txt
python -m app.preflight --deps-only
python -m app.launch
