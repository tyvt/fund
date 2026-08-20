@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

cd /d "%~dp0.."
set "VENV_DIR=%CD%\rqalpha_env"
set "PY=%VENV_DIR%\Scripts\python.exe"
set "PIP=%VENV_DIR%\Scripts\pip.exe"

echo === RQAlpha 环境搭建（红利低波迁移 Step 1）===
echo 项目目录: %CD%
echo.

if not exist "%PY%" (
    echo [1/4] 创建虚拟环境 rqalpha_env ...
    python -m venv rqalpha_env
    if errorlevel 1 (
        echo 创建虚拟环境失败
        exit /b 1
    )
) else (
    echo [1/4] 虚拟环境已存在: %VENV_DIR%
)

echo [2/4] 安装 RQAlpha ...
"%PIP%" install --upgrade pip
"%PIP%" install rqalpha pandas numpy duckdb akshare baostock
if errorlevel 1 (
    echo pip 安装失败，可尝试: pip install -i https://pypi.tuna.tsinghua.edu.cn/simple rqalpha
    exit /b 1
)

echo [3/4] 下载 A 股数据包（约 1GB，首次较慢）...
set "RQALPHA_DATA_DIR=D:\rqalpha"
if not exist "%RQALPHA_DATA_DIR%" mkdir "%RQALPHA_DATA_DIR%"
"%VENV_DIR%\Scripts\rqalpha.exe" download-bundle -d "%RQALPHA_DATA_DIR%"
if errorlevel 1 (
    echo 数据包下载失败，请检查网络后重试
    exit /b 1
)

echo [4/4] 验证安装 ...
"%VENV_DIR%\Scripts\rqalpha.exe" version
echo.
echo 完成。运行回测:
echo   run_rqalpha_backtest.bat
echo 或:
echo   rqalpha_env\Scripts\python.exe -m dividend_lowvol_rotation.rqalpha.run_backtest --years 10 --end 2025-08-01
endlocal
