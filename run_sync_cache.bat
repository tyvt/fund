@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python "%~dp0sync_data_cache.py" >> "%~dp0logs\sync_cache.log" 2>&1
