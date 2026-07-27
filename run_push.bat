@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python "%~dp0push.py" >> "%~dp0logs\push.log" 2>&1
