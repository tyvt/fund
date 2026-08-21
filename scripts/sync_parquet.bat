@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo === Parquet 数据湖同步 ===
echo 默认：StockDB 全量 scope=all（约 7563 只），2000-01-01 至今，--resume 增量
echo 仅 A 股：python scripts\export_to_parquet.py --scope a_share --resume
echo 试点：python scripts\export_to_parquet.py --limit 100
echo.

python scripts\export_to_parquet.py %*
if errorlevel 1 exit /b 1

echo.
echo 同步完成。日志：logs\export_to_parquet.log
endlocal
