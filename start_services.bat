@echo off

REM 语音交互APP启动脚本 (Windows)

REM 检查Python环境
echo 检查Python环境...
python --version
if %errorlevel% neq 0 (
    echo Python未安装，请先安装Python 3.8+
    pause
    exit /b 1
)

REM 检查Redis是否安装
if not exist "C:\Program Files\Redis\redis-server.exe" (
    echo Redis未安装，请先下载并安装Redis
    echo 下载地址: https://github.com/microsoftarchive/redis/releases
    pause
    exit /b 1
)

REM 启动Redis（如果未运行）
echo 启动Redis服务...
tasklist | findstr "redis-server" > nul
if %errorlevel% neq 0 (
    start "Redis Server" "C:\Program Files\Redis\redis-server.exe"
    echo Redis服务已启动
) else (
    echo Redis服务已在运行
)

REM 启动API Gateway
echo 启动API Gateway...
start "API Gateway" cmd /c "cd api-gateway && python app.py"

REM 启动核心AI服务
echo 启动核心AI服务...
start "Core AI Service" cmd /c "cd ai-service && python main.py"

REM 等待服务启动
echo 等待服务启动...
timeout /t 5 /nobreak > nul

echo 所有服务已启动！
echo - API Gateway: http://localhost:8000
echo - 核心AI服务: http://localhost:8080
echo - Redis: localhost:6379

echo.
echo 服务状态：
tasklist | findstr "python redis-server"

echo.
echo 按任意键退出...
pause > nul