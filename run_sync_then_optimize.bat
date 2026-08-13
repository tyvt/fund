@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
if not exist "%~dp0logs" mkdir "%~dp0logs"
python "%~dp0scripts\run_sync_then_optimize.py" %*
