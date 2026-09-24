@echo off
setlocal
cd /d "%~dp0"

python --version >nul 2>nul
if not errorlevel 1 (
  set "PY_CMD=python"
) else (
  py -3 --version >nul 2>nul
  if errorlevel 1 (
    echo Python 3.11 or newer was not found. Install Python and enable its PATH entry.
    goto fail
  )
  set "PY_CMD=py -3"
)

%PY_CMD% -c "import sys; sys.exit(sys.version_info < (3, 11))"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  goto fail
)

%PY_CMD% -m app.preflight --deps-only >nul 2>nul
if errorlevel 1 (
  echo Installing missing or outdated Python dependencies...
  %PY_CMD% -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
%PY_CMD% -m app.preflight --deps-only
if errorlevel 1 goto fail

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo WARNING: FFmpeg is not in PATH. The dashboard can open, but audio building needs FFmpeg.
)

%PY_CMD% -m app.launch
if errorlevel 1 goto fail
exit /b 0

:fail
echo Startup failed. Review the error above; this window will remain open.
pause
exit /b 1
