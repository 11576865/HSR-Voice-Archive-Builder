#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"

command -v python >/dev/null 2>&1 || pkg install -y python
command -v ffmpeg >/dev/null 2>&1 || pkg install -y ffmpeg

if ! command -v 7zz >/dev/null 2>&1 && ! command -v 7z >/dev/null 2>&1; then
  pkg install -y 7zip || pkg install -y p7zip
fi

python -m app.preflight --deps-only >/dev/null 2>&1 || python -m pip install -r requirements-termux.txt
python -m app.preflight --deps-only
python -m app.launch --lan --no-browser
