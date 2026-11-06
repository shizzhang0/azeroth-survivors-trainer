@echo off
rem Double-click to open the trainer window (needs uv on PATH).
cd /d "%~dp0"
rem Chinese labels print as UTF-8 even when the output is piped or redirected.
set PYTHONUTF8=1
uv run --no-project --with numpy python src\gui.py
if errorlevel 1 pause
