@echo off
rem Card auto-pick: picks level-up and treasure cards by the lists in AZS-cards.txt.
rem Runs on its own; AZS-Launcher.bat in the game folder starts it next to remote.bat
rem in the same window and passes --cards and --game-log (see README).
cd /d "%~dp0"
rem Chinese card names print as UTF-8 even when the output is piped or redirected.
set PYTHONUTF8=1
uv run --no-project --with numpy python src\cardpick.py %*
if errorlevel 1 pause
