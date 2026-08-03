@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=红利指数投资信号"
set "RUN_TIME=14:00"

if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"

for /f "delims=" %%Z in ('tzutil /g 2^>nul') do set "SYS_TZ=%%Z"
if /i not "%SYS_TZ%"=="China Standard Time" goto warn_timezone
goto after_timezone_warn

:warn_timezone
echo 警告: 系统时区为 %SYS_TZ%，非上海时区（China Standard Time）。
echo 定时任务将按系统本地时间 %RUN_TIME% 执行，请先在系统设置中切换为「UTC+08:00 北京，重庆，香港特别行政区，乌鲁木齐」。
echo.

:after_timezone_warn
if not exist "%SCRIPT_DIR%push.env" goto setup_env
goto after_env_check

:setup_env
echo 请先复制 push.example.env 为 push.env 并配置 SERVERCHAN_SENDKEY
echo.
copy "%SCRIPT_DIR%push.example.env" "%SCRIPT_DIR%push.env"
notepad "%SCRIPT_DIR%push.env"
echo.
echo 配置完成后请重新运行此脚本。
pause
exit /b 1

:after_env_check
for %%T in ("H30269投资信号" "H50040投资信号" "红利指数投资信号") do (
    schtasks /query /tn %%~T >nul 2>&1
    if !errorlevel!==0 (
        schtasks /delete /tn %%~T /f >nul
    )
)

schtasks /create ^
    /tn %TASK_NAME% ^
    /tr %SCRIPT_DIR%run_push.bat ^
    /sc weekly ^
    /d MON,TUE,WED,THU,FRI ^
    /st %RUN_TIME% ^
    /f

if %errorlevel%==0 goto task_ok
goto task_fail

:task_ok
echo.
echo 定时任务创建成功！
echo 任务名称: %TASK_NAME%
echo 执行时间: 周一至周五 %RUN_TIME%（系统本地时间，上海时区 UTC+8）
echo 执行脚本: %SCRIPT_DIR%run_push.bat
echo 日志文件: %SCRIPT_DIR%logs\push.log
echo.
echo 已自动清理旧版单独指数任务（如存在）。
echo.
echo 本地查看报告:
echo   python report.py
echo 测试推送（将发送微信）:
echo   python push.py
goto end

:task_fail
echo 定时任务创建失败，请尝试右键「以管理员身份运行」此脚本。

:end
pause

