[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = 'C:\o\Scripts\pythonw.exe'
$scriptPath = Join-Path $projectRoot 'run.py'
$configPath = Join-Path $projectRoot 'config.json'
$taskName = 'Upload_TOSHIBA_OCI_20260925_0900'

if (-not (Test-Path -LiteralPath $pythonPath)) { throw "No existe $pythonPath" }
if (-not (Test-Path -LiteralPath $scriptPath)) { throw "No existe $scriptPath" }

$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder = $scheduler.GetFolder('\')
$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = 'Respaldo deduplicado y reanudable del TOSHIBA a OCI Object Storage.'
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
$definition.Settings.RestartCount = 12
$definition.Settings.RestartInterval = 'PT30M'

$trigger = $definition.Triggers.Create(1)
$trigger.StartBoundary = '2026-09-25T09:00:00'
$trigger.Enabled = $true

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
    Programa = $registered.Definition.Actions.Item(1).Path
    Argumentos = $registered.Definition.Actions.Item(1).Arguments
    Oculta = $registered.Definition.Settings.Hidden
    Reintentos = $registered.Definition.Settings.RestartCount
    Intervalo = $registered.Definition.Settings.RestartInterval
} | ConvertTo-Json
