@echo off
rem CodeAgent 可视化安装向导启动器(Windows PowerShell 5.1 自带,无需任何第三方依赖)
rem 用法:双击本文件。
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0install-gui.ps1"
if errorlevel 1 (
  echo.
  echo 安装向导异常退出。可手动运行:
  echo   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gui.ps1"
  pause
)
