@echo off
rem Background agent for the in-game controller: start it, then type /azs in a run.
rem Runs on its own; AZS-Launcher.bat in the game folder also copies the addon in
rem and passes --preset (see README).
cd /d "%~dp0"
uv run --no-project --with numpy python src\agent.py %*
if errorlevel 1 pause
