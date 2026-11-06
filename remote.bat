@echo off
rem Background agent for the in-game controller: start it, then type /azs in a run.
cd /d "%~dp0"
rem Chinese labels print as UTF-8 even when the output is piped or redirected.
set PYTHONUTF8=1
uv run --no-project --with numpy python src\agent.py %*
if errorlevel 1 pause
