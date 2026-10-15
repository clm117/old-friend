@echo off
REM 语伴 一键启动：ai-service + api-gateway + nginx + localtunnel
REM 双击运行或命令行执行。窗口保持打开，关闭窗口即停止隧道。

chcp 65001 >nul
set PYTHON="D:\Program Files (x86)\Python 3.11\python.exe"
set ROOT=D:\ProgramFile\语伴
set NGINX=%ROOT%\infrastructure\nginx\nginx-1.26.3

echo [1/5] 停止旧进程...
taskkill /IM nginx.exe /F >nul 2>&1
for %%P in (8080 8005 8084) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%%P " ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo [2/5] 启动 ai-service (:8084)...
cd /d %ROOT%\ai-service
start "yuban-ai-service" %PYTHON% main.py

echo [3/5] 启动 api-gateway (:8005)...
cd /d %ROOT%\api-gateway
start "yuban-gateway" %PYTHON% app.py

echo [4/5] 启动 nginx (:8080)...
cd /d %NGINX%
start "yuban-nginx" %NGINX%\nginx.exe -c conf\yuban.conf

echo [5/5] 启动 HTTPS 隧道（Cloudflare，无验证页）...
echo.
echo ================================================
echo   等待 20 秒后服务全部就绪
echo   本地:  http://localhost:8080
echo   隧道地址显示在下方（trycloudflare.com 开头）
echo   手机浏览器直接打开即可，无需输 IP
echo ================================================
echo.
cd /d %ROOT%\infrastructure\cloudflared
%ROOT%\infrastructure\cloudflared\cloudflared.exe tunnel --url http://localhost:8080
