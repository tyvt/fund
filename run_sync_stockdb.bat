@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "%~dp0logs" mkdir "%~dp0logs"
python "%~dp0sync_stockdb_to_duckdb.py" >> "%~dp0logs\sync_stockdb.log" 2>&1
