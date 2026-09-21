$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'recovery_common.ps1')
$Lock = Enter-ServiceControlLock $Root 'watchdog'
try {
    $Path = Join-Path (Get-RecoveryDirectory $Root) 'settings.json'
    $Settings = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $Task = Get-ScheduledTask -TaskName $Settings.task_name -ErrorAction SilentlyContinue
    $Expected = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'watch_zhuorui_services.ps1')
    if ($Task -and $Task.Actions.Arguments -ne $Expected) { throw 'Task identity did not match; it was left untouched.' }
    $Settings.enabled=$false
    Write-RecoveryJson $Path $Settings
    if ($Task) { Unregister-ScheduledTask -TaskName $Settings.task_name -Confirm:$false }
    Write-Host 'Automatic recovery disabled. Running components and their Start/Stop settings were retained.'
} finally { $Lock.Dispose() }
