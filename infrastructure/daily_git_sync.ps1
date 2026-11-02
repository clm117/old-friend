$Repo = "D:\ProgramFile\语伴"
$Branch = "main"
$ErrorActionPreference = "Continue"
$ts = Get-Date -Format "yyyy-MM-dd HH:mm"
$log = Join-Path $Repo "infrastructure\daily_sync.log"
function Write-Log([string]$msg) { Add-Content -Path $log -Value "[$(Get-Date -Format 'HH:mm:ss')] $msg" -Encoding UTF8 }
Set-Location -Path $Repo
Write-Log "=== Daily sync start ($ts) ==="
$out = git -C $Repo status --porcelain 2>&1 | Out-String
if ($out.Trim().Length -eq 0) { Write-Log "Clean, skip."; Write-Log "=== End ==="; exit 0 }
Write-Log "Changes detected"
git -C $Repo add -A 2>&1 | Out-Null
$msg = "chore: daily auto-sync $ts"
$c = git -C $Repo commit -m $msg 2>&1
if ($LASTEXITCODE -ne 0) { Write-Log "commit failed: $c"; Write-Log "=== End fail ==="; exit 1 }
Write-Log "commit OK"
$p = git -C $Repo push origin $Branch 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Log "push failed, retry in 15s"
    Start-Sleep 15
    $p2 = git -C $Repo push origin $Branch 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Log "push still failed, commit kept local" } else { Write-Log "push OK on retry" }
} else { Write-Log "push OK" }
Write-Log "=== End ==="