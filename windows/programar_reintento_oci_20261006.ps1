[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = 'Upload_TOSHIBA_OCI_20261006_RETRY'
$previousName = 'Upload_TOSHIBA_OCI_20260930_TO_20261002_RETRY'
$previous = Get-ScheduledTask -TaskName $previousName
$action = New-ScheduledTaskAction -Execute 'C:\o\Scripts\pythonw.exe' -Argument (
    '-X utf8 "' + (Join-Path $projectRoot 'run.py') + '" --config "' +
    (Join-Path $projectRoot 'config.json') + '" backup'
) -WorkingDirectory $projectRoot
$start = [datetime]'2026-10-06T09:00:00'
$triggers = @(foreach ($index in 0..11) {
    $at = $start.AddMinutes(30 * $index)
    $trigger = New-ScheduledTaskTrigger -Once -At $at
    $trigger.StartBoundary = $at.ToString('yyyy-MM-ddTHH:mm:ss') + '-05:00'
    $trigger
})
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([timespan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$definition = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings `
    -Principal $previous.Principal -Description 'Completar contenidos pendientes de TOSHIBA en OCI el 6 de octubre, 09:00 a 14:30 Lima.'
Register-ScheduledTask -TaskName $taskName -InputObject $definition -Force | Out-Null
Disable-ScheduledTask -TaskName $previousName | Out-Null
$registered = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -InputObject $registered
if ($registered.Triggers.Count -ne 12) { throw 'No se registraron las doce activaciones esperadas.' }
[pscustomobject]@{
    Name = $registered.TaskName
    State = [string]$registered.State
    NextRun = $info.NextRunTime.ToString('o')
    Times = @($registered.Triggers | ForEach-Object { $_.StartBoundary })
    Execute = $registered.Actions.Execute
    Arguments = $registered.Actions.Arguments
    User = $registered.Principal.UserId
    LogonType = [string]$registered.Principal.LogonType
    Hidden = $registered.Settings.Hidden
    StartWhenAvailable = $registered.Settings.StartWhenAvailable
    MultipleInstances = [string]$registered.Settings.MultipleInstances
    ExecutionTimeLimit = $registered.Settings.ExecutionTimeLimit
} | ConvertTo-Json -Depth 3
