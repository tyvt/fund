@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=rqalpha_env\Scripts\python.exe"
if exist "%PY%" (
    "%PY%" -c "import rqalpha" 2>nul
    if not errorlevel 1 goto :run
)

set "PY=python"
"%PY%" -c "import rqalpha" 2>nul
if errorlevel 1 (
    echo 未找到已安装 rqalpha 的 Python。请先 pip install rqalpha 或运行 scripts\setup_rqalpha_env.bat
    exit /b 1
)

:run
"%PY%" -m dividend_lowvol_rotation.rqalpha.run_backtest %*
