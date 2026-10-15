@echo off
REM 语伴 nginx 启动脚本（前端静态 + API/WS 反代，端口 8080）
cd /d "%~dp0nginx-1.26.3"
nginx.exe -c conf/yuban.conf
if %errorlevel%==0 (
    echo [OK] nginx 已启动，手机/浏览器访问:
    echo      http://本机IP:8080/
    echo      测试音频目录: http://本机IP:8080/test_outputs/
) else (
    echo [FAIL] nginx 启动失败，请检查 logs/error.log
)
timeout /t 3 >nul
