@echo off
rem Double-click to open the trainer window (needs uv on PATH).
cd /d "%~dp0"
uv run --no-project --with numpy python src\gui.py
if errorlevel 1 pause
