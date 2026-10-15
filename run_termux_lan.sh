#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"

command -v python >/dev/null 2>&1 || pkg install -y python
command -v ffmpeg >/dev/null 2>&1 || pkg install -y ffmpeg
# Termux 2.18.x splits fc-match into fontconfig-utils, not fontconfig.
command -v fc-match >/dev/null 2>&1 || pkg install -y fontconfig-utils
if ! command -v fc-match >/dev/null 2>&1; then
  echo "Missing fc-match: install Termux fontconfig-utils (pkg install -y fontconfig-utils)." >&2
  exit 1
fi

if ! command -v 7zz >/dev/null 2>&1 && ! command -v 7z >/dev/null 2>&1; then
  pkg install -y 7zip || pkg install -y p7zip
fi

# Termux ships Pillow in python-pillow; without it imported font metrics
# silently fall back to approximate per-character width ratios.
python -c "from PIL import ImageFont" >/dev/null 2>&1 || pkg install -y python-pillow
if ! python -c "from PIL import ImageFont" >/dev/null 2>&1; then
  echo "Missing imported-font measurement support: pkg install -y python-pillow" >&2
  exit 1
fi
python -m app.preflight --deps-only >/dev/null 2>&1 || python -m pip install -r requirements-termux.txt
python -m app.preflight --deps-only
python -m app.launch --lite --lan --no-browser
