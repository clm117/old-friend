$serviceName = "cloudflared"
$exePath = "D:\ProgramFile\语伴\infrastructure\cloudflared\cloudflared.exe"
$configPath = "d:\ProgramFile\语伴\infrastructure\cloudflared\config.yml"
$newBinPath = "`"$exePath`" --config `"$configPath`""

# Force kill cloudflared processes
taskkill /F /IM cloudflared.exe 2>$null
Start-Sleep -Seconds 3

# Delete existing service
sc.exe delete $serviceName 2>$null
Start-Sleep -Seconds 2

# Create service with correct binPath
sc.exe create $serviceName binPath= $newBinPath start= auto DisplayName= "Cloudflared agent"
Start-Sleep -Seconds 2

# Start the service
sc.exe start $serviceName
Start-Sleep -Seconds 3

# Verify
Get-Service -Name $serviceName | Format-List Name, Status, StartType
Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'" | Select-Object PathName
