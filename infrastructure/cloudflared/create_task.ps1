# Create a scheduled task to start cloudflared tunnel at user logon
$taskName = "YubanCloudflareTunnel"
$exePath = "D:\cf-tunnel\cloudflared.exe"
$configPath = "D:\cf-tunnel\config.yml"
$action = New-ScheduledTaskAction -Execute $exePath -Argument "tunnel --config `"$configPath`" run"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

# Register the task
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force

# Verify
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
