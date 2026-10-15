# Create cloudflared service using New-Service (better Unicode support)
$serviceName = "cloudflared"
$exePath = "D:\ProgramFile\语伴\infrastructure\cloudflared\cloudflared.exe"
$configPath = "d:\ProgramFile\语伴\infrastructure\cloudflared\config.yml"

# Kill any existing cloudflared processes
taskkill /F /IM cloudflared.exe 2>$null
Start-Sleep -Seconds 2

# Delete existing service if any
sc.exe delete $serviceName 2>$null
Start-Sleep -Seconds 3

# Create the service with config argument
$binaryPath = "`"$exePath`" --config `"$configPath`""
New-Service -Name $serviceName -BinaryPathName $binaryPath -DisplayName "Cloudflared agent" -StartupType Automatic -Description "Cloudflare Tunnel for yuban"

Start-Sleep -Seconds 2

# Start the service
Start-Service -Name $serviceName

Start-Sleep -Seconds 3

# Verify
Get-Service -Name $serviceName | Format-List Name, Status, StartType
Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'" | Select-Object PathName
