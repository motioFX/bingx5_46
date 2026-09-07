# Register Technocore Keepalive Task in Windows Task Scheduler (Runs every 4 hours)
$taskName = "TechnocoreKeepaliveAgent"
$scriptPath = Join-Path $PSScriptRoot "keepalive.py"
$pythonPath = (Get-Command python).Source

$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "`"$scriptPath`" --once" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "Technocore DID and Activity Refresh Service (Every 4h)" -Force
Write-Host "Task '$taskName' registered successfully in Windows Task Scheduler!"