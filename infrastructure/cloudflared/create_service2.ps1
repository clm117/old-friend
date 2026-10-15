$serviceName = "cloudflared"
$exePath = "D:\cf-tunnel\cloudflared.exe"
$configPath = "D:\cf-tunnel\config.yml"

# Kill existing cloudflared processes
taskkill /F /IM cloudflared.exe 2>$null
Start-Sleep -Seconds 2

# Delete existing service
sc.exe delete $serviceName 2>$null
Start-Sleep -Seconds 3

# Create service with ASCII paths (no Chinese characters)
$binaryPath = "`"$exePath`" --config `"$configPath`""
New-Service -Name $serviceName -BinaryPathName $binaryPath -DisplayName "Cloudflared agent" -StartupType Automatic -Description "Cloudflare Tunnel for yuban"

Start-Sleep -Seconds 2

# Start the service
Start-Service -Name $serviceName

Start-Sleep -Seconds 5

# Verify
Get-Service -Name $serviceName | Format-List Name, Status, StartType
$regPath = "HKLM:\SYSTEM\CurrentControlSet\Services\cloudflared"
if (Test-Path $regPath) {
    $props = Get-ItemProperty $regPath
    Write-Output "ImagePath: $($props.ImagePath)"
}
