@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set ARCHIVE_DATE=%%i
set "ARCHIVE_DIR=output\_archive\%ARCHIVE_DATE%"

echo 归档 output/ 到 %ARCHIVE_DIR% ...
if not exist "%ARCHIVE_DIR%\backtest" mkdir "%ARCHIVE_DIR%\backtest"
if not exist "%ARCHIVE_DIR%\dividend_lowvol" mkdir "%ARCHIVE_DIR%\dividend_lowvol"

if exist "output\backtest\*" move /Y "output\backtest\*" "%ARCHIVE_DIR%\backtest\" >nul 2>&1
if exist "output\dividend_lowvol\*" move /Y "output\dividend_lowvol\*" "%ARCHIVE_DIR%\dividend_lowvol\" >nul 2>&1
if exist "output\dividend_lowvol_report.md" move /Y "output\dividend_lowvol_report.md" "%ARCHIVE_DIR%\" >nul 2>&1

echo 完成。重跑回测: python backtest.py -m inception
