@echo off
REM 语伴 nginx 停止脚本
cd /d "%~dp0nginx-1.26.3"
nginx.exe -s stop
echo [OK] nginx 已停止
timeout /t 2 >nul
