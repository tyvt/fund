@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=投资信号数据预拉取"
set "RUN_TIME=09:30"

if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"

for /f "delims=" %%Z in ('tzutil /g 2^>nul') do set "SYS_TZ=%%Z"
if /i not "%SYS_TZ%"=="China Standard Time" goto warn_timezone
goto after_timezone_warn

:warn_timezone
echo 警告: 系统时区为 %SYS_TZ%，非上海时区（China Standard Time）。
echo 定时任务将按系统本地时间 %RUN_TIME% 执行，请先在系统设置中切换为「UTC+08:00 北京，重庆，香港特别行政区，乌鲁木齐」。
echo.

:after_timezone_warn
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel%==0 (
    schtasks /delete /tn "%TASK_NAME%" /f >nul
)

schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "%SCRIPT_DIR%run_sync_cache.bat" ^
    /sc daily ^
    /st %RUN_TIME% ^
    /f

if %errorlevel%==0 goto task_ok
goto task_fail

:task_ok
echo.
echo 定时任务创建成功！
echo 任务名称: %TASK_NAME%
echo 执行时间: 每天 %RUN_TIME%（系统本地时间，上海时区 UTC+8）
echo 执行脚本: %SCRIPT_DIR%run_sync_cache.bat
echo 日志文件: %SCRIPT_DIR%logs\sync_cache.log
echo.
echo 手动预拉缓存:
echo   python sync_data_cache.py
goto end

:task_fail
echo 定时任务创建失败，请尝试右键「以管理员身份运行」此脚本。

:end
pause
