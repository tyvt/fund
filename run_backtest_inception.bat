@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo === 买入信号 + 买卖波段全量回测（Markdown + HTML）===
python "%~dp0backtest.py" --mode inception %*
if errorlevel 1 exit /b 1

echo.
echo 完成。输出目录: output\backtest\
