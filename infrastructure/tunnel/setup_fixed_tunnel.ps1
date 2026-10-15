# 语伴 - 固定域名隧道配置方案（一键脚本）
# 前提: 1) 域名 NS 已托管到 Cloudflare  2) 已注册 Cloudflare 账号
# 用法: 右键"使用 PowerShell 运行", 或在 PowerShell 中执行:
#   powershell -ExecutionPolicy Bypass -File setup_fixed_tunnel.ps1 -Domain yuban.yourdomain.com

param(
    [Parameter(Mandatory = $false)]
    [string]$Domain = "yuban.moonexplore.cn",
    [int]$LocalPort = 8080
)

$ErrorActionPreference = "Stop"
$cloudflaredDir = Join-Path $PSScriptRoot "..\cloudflared"
$cloudflared = Join-Path $cloudflaredDir "cloudflared.exe"
$cfDir = Join-Path $env:USERPROFILE ".cloudflared"

if (-not (Test-Path $cloudflared)) { Write-Host "[错误] 未找到 cloudflared.exe" -ForegroundColor Red; exit 1 }
if ($Domain -notmatch '^[a-z0-9][a-z0-9\.\-]+$') { Write-Host "[错误] 域名格式不对" -ForegroundColor Red; exit 1 }

Write-Host "`n===== 语伴固定域名隧道安装 =====" -ForegroundColor Cyan
Write-Host "目标域名: https://$Domain -> 本机 :$LocalPort`n"

# ---------- 步骤 1/5 登录授权 ----------
Write-Host "[1/5] 浏览器登录 Cloudflare 授权..." -ForegroundColor Yellow
$certPem = Join-Path $cfDir "cert.pem"
if (Test-Path $certPem) {
    Write-Host "      已存在授权 (cert.pem), 跳过登录"
} else {
    & $cloudflared tunnel login
    if (-not (Test-Path $certPem)) { Write-Host "[错误] 授权失败, 未生成 cert.pem" -ForegroundColor Red; exit 1 }
}

# ---------- 步骤 2/5 创建命名隧道 ----------
Write-Host "[2/5] 创建命名隧道 yuban..." -ForegroundColor Yellow
$exists = (& $cloudflared tunnel list 2>$null | Select-String -Pattern "^\S+\s+(yuban)\s" -Quiet)
if ($exists) {
    Write-Host "      隧道 yuban 已存在, 复用"
    $tunnelId = (& $cloudflared tunnel list --output json | ConvertFrom-Json |
        Where-Object { $_.name -eq "yuban" } | Select-Object -First 1).id
} else {
    $createOut = & $cloudflared tunnel create yuban
    Write-Host ($createOut -join "`n")
    $tunnelId = ($createOut | Select-String -Pattern "([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})").Matches[0].Value
}
if (-not $tunnelId) { Write-Host "[错误] 未获取到 Tunnel ID" -ForegroundColor Red; exit 1 }
Write-Host "      Tunnel ID: $tunnelId"

# ---------- 步骤 3/5 生成 config.yml ----------
Write-Host "[3/5] 生成配置 $cfDir\config.yml ..." -ForegroundColor Yellow
if (-not (Test-Path $cfDir)) { New-Item -ItemType Directory -Force -Path $cfDir | Out-Null }
$config = @"
tunnel: $tunnelId
credentials-file: $cfDir\$tunnelId.json

ingress:
  - hostname: $Domain
    service: http://localhost:$LocalPort
    originRequest:
      connectTimeout: 30s
  - service: http_status:404
"@
Set-Content -Path (Join-Path $cfDir "config.yml") -Value $config -Encoding UTF8

# ---------- 步骤 4/5 绑定 DNS ----------
Write-Host "[4/5] 绑定 DNS: $Domain -> 隧道 yuban ..." -ForegroundColor Yellow
& $cloudflared tunnel route dns yuban $Domain

# ---------- 步骤 5/5 注册 Windows 服务 (开机自启) ----------
Write-Host "[5/5] 注册 Windows 服务 cloudflared (开机自启)..." -ForegroundColor Yellow
$svc = Get-Service -Name "cloudflared" -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "      服务已存在, 重启生效..."
    Restart-Service -Name "cloudflared" -Force
} else {
    & $cloudflared service install
    Start-Service -Name "cloudflared" -ErrorAction SilentlyContinue
}

# ---------- 验证 ----------
Write-Host "`n等待隧道建立 (10s)..." -ForegroundColor Cyan
Start-Sleep -Seconds 10
$svcState = (Get-Service -Name "cloudflared" -ErrorAction SilentlyContinue).Status
Write-Host "服务状态: $svcState"
Write-Host "服务端验证: https://$Domain" -ForegroundColor Green
$probe = $null
try { $probe = Invoke-WebRequest -Uri "https://$Domain/health" -UseBasicParsing -TimeoutSec 25 } catch {}
if ($probe -and $probe.StatusCode -eq 200) {
    Write-Host "`n✔ 安装成功! 固定地址: https://$Domain" -ForegroundColor Green
    Write-Host $probe.Content
} else {
    Write-Host "`n⚠ 隧道已注册但暂未验证通过, 可能原因:" -ForegroundColor Yellow
    Write-Host "  1. 域名 NS 还未生效 (新托管 5 分钟~2 小时), 稍后手动重试"
    Write-Host "  2. DNS 记录同步延迟, 去 Cloudflare 控制台看 CNAME $Domain -> $tunnelId.cfargotunnel.com"
    Write-Host "  3. 服务日志: C:\Windows\System32\config\systemprofile\.cloudflared\cloudflared.log"
}
Write-Host "`n链路: 手机 -> https://$Domain -> Cloudflare -> 本机 cloudflared 服务 -> nginx:$LocalPort -> 网关/AI" -ForegroundColor Cyan
