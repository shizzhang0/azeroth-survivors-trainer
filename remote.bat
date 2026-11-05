@echo off
rem Background agent for the in-game controller: start it, then type /azs in a run.
cd /d "%~dp0"
uv run --no-project --with numpy python src\agent.py
if errorlevel 1 pause
