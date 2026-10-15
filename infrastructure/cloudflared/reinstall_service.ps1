$serviceName = "cloudflared"
$exePath = "D:\ProgramFile\语伴\infrastructure\cloudflared\cloudflared.exe"

# Force kill cloudflared processes
taskkill /F /IM cloudflared.exe 2>$null
Start-Sleep -Seconds 3

# Delete existing service
sc.exe delete $serviceName 2>$null
Start-Sleep -Seconds 3

# Install service using cloudflared's own installer (default binPath, no Chinese issues)
& $exePath service install
Start-Sleep -Seconds 3

# Verify installation
Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'" | Select-Object Name, State, StartMode, PathName, StartName | Format-List
