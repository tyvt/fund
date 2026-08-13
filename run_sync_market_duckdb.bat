@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "%~dp0logs" mkdir "%~dp0logs"
python "%~dp0sync_market_duckdb.py" >> "%~dp0logs\sync_market_duckdb.log" 2>&1
