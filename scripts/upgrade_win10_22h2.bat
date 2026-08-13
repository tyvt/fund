@echo off
chcp 65001 >nul
echo ============================================
echo  Windows 10 升级到 22H2（Docker Desktop 需要）
echo  请右键本文件 -^> 以管理员身份运行
echo ============================================
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 需要管理员权限！请右键此文件，选择「以管理员身份运行」
    pause
    exit /b 1
)

echo [1/4] 启用 Windows Update 服务...
sc config wuauserv start= demand
sc config bits start= demand
net start wuauserv
net start bits

echo.
echo [2/4] 触发 Windows 更新扫描...
UsoClient StartScan
timeout /t 20 /nobreak >nul
UsoClient StartDownload
timeout /t 30 /nobreak >nul
UsoClient StartInstall

echo.
echo [3/4] 打开 Windows 更新设置...
start ms-settings:windowsupdate

echo.
echo [4/4] 若更新列表没有 22H2，将下载官方「更新助手」...
set "ASSISTANT=%TEMP%\Windows10Upgrade9252.exe"
if not exist "%ASSISTANT%" (
    echo 正在下载 Windows 10 更新助手...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/?LinkID=799445' -OutFile '%ASSISTANT%' -UseBasicParsing"
)

if exist "%ASSISTANT%" (
    echo 启动更新助手（按界面提示操作）...
    start "" "%ASSISTANT%"
) else (
    echo 下载失败，请手动访问:
    echo https://www.microsoft.com/software-download/windows10
)

echo.
echo ============================================
echo  完成后系统版本应为 22H2（Build 19045+）
echo  然后即可安装 Docker Desktop
echo ============================================
pause
