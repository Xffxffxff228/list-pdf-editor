@echo off
setlocal
cd /d "%~dp0"
where uv >nul 2>nul
if not errorlevel 1 (
  uv run --with "pymupdf>=1.24,<2" server.py
  goto :eof
)
where py >nul 2>nul
if errorlevel 1 (
  echo Install Python 3.10 or newer, then run this file again.
  echo https://www.python.org/downloads/
  pause
  exit /b 1
)
py -m pip install -r requirements.txt
if errorlevel 1 (
  pause
  exit /b 1
)
py server.py
pause

