@echo off
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found in PATH.
  pause
  exit /b 1
)
python -m app.preflight --deps-only >nul 2>nul
if errorlevel 1 (
  echo Installing missing or outdated Python dependencies...
  python -m pip install -r requirements.txt || exit /b 1
)
python -m app.preflight --deps-only || exit /b 1
python -m app.launch
