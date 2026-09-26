[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = 'C:\o\Scripts\pythonw.exe'
$scriptPath = Join-Path $projectRoot 'run.py'
$configPath = Join-Path $projectRoot 'config.json'
$taskName = 'Upload_TOSHIBA_OCI_20260928_RETRY'

$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = 'Reintenta completar desde el TOSHIBA solo los contenidos ausentes en OCI.'
$definition.RegistrationInfo.Author = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$definition.Principal.LogonType = 3
$definition.Principal.RunLevel = 0
$definition.Settings.Enabled = $true
$definition.Settings.Hidden = $true
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.WakeToRun = $true
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.MultipleInstances = 2
$definition.Settings.ExecutionTimeLimit = 'PT0S'

$trigger = $definition.Triggers.Create(1)
$trigger.StartBoundary = '2026-09-28T09:00:00'
$trigger.Enabled = $true
$trigger.Repetition.Interval = 'PT30M'
$trigger.Repetition.Duration = 'PT6H'
$trigger.Repetition.StopAtDurationEnd = $false

$action = $definition.Actions.Create(0)
$action.Path = $pythonPath
$action.Arguments = '-X utf8 "' + $scriptPath + '" --config "' + $configPath + '" backup'
$action.WorkingDirectory = $projectRoot

$registered = $folder.RegisterTaskDefinition(
    $taskName, $definition, 6, $definition.Principal.UserId, $null, 3, $null
)

[pscustomobject]@{
    Nombre = $registered.Name
    Habilitada = $registered.Enabled
    ProximaEjecucion = $registered.NextRunTime.ToString('o')
    Inicio = $registered.Definition.Triggers.Item(1).StartBoundary
    Intervalo = $registered.Definition.Triggers.Item(1).Repetition.Interval
    Duracion = $registered.Definition.Triggers.Item(1).Repetition.Duration
    Programa = $registered.Definition.Actions.Item(1).Path
} | ConvertTo-Json
