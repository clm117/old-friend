@echo off
REM 语伴固定域名隧道一键安装 (双击运行, 需已购买域名并托管到 Cloudflare)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_fixed_tunnel.ps1" %*
pause
