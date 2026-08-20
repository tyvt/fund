@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先 rqalpha_env；若无 rqalpha 则回退系统 Python（需已 pip install rqalpha）
set "PY=rqalpha_env\Scripts\python.exe"
if exist "%PY%" (
    "%PY%" -c "import rqalpha" 2>nul
    if not errorlevel 1 goto :run
)

set "PY=python"
"%PY%" -c "import rqalpha" 2>nul
if errorlevel 1 (
    echo 未找到已安装 rqalpha 的 Python。
    echo   1^) pip install rqalpha
    echo   2^) 或运行 scripts\setup_rqalpha_env.bat
    echo   3^) 或临时: set DLV_BACKTEST_PRICE_SOURCE=duckdb
    exit /b 1
)

:run
"%PY%" -m dividend_lowvol_rotation.backtest %*
