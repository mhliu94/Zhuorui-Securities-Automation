param(
    [string]$ConfigPath = '.\zhuorui_config.json',
    [ValidateSet('api', 'ui')][string]$Backend = 'api',
    [switch]$Recovery
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'listener_common.ps1')
. (Join-Path $PSScriptRoot 'recovery_common.ps1')
$ControlLock = Enter-ServiceControlLock $Root 'listener'
try {
if ($Recovery) {
    $Intent = Read-RecoveryIntent $Root 'api'
    if ($Backend -ne 'api' -or -not $Intent -or -not $Intent.desired_running) { return }
    $ConfigPath = [string]$Intent.parameters.ConfigPath
}
$Paths = Get-ListenerPaths -ProjectRoot $Root -Backend $Backend
$ResolvedConfig = Resolve-ListenerConfigPath -Value $ConfigPath -BaseDirectory $Root
$Config = Get-Content -LiteralPath $ResolvedConfig -Raw | ConvertFrom-Json
$ConfigDirectory = Split-Path -Parent $ResolvedConfig
# Prevent two consumers controlling the same account through different transports.
$OtherBackend = if ($Backend -eq 'api') { 'ui' } else { 'api' }
$OtherPaths = Get-ListenerPaths -ProjectRoot $Root -Backend $OtherBackend
$Other = Get-TrackedListener -Paths $OtherPaths -Backend $OtherBackend
if ($Other) {
    throw "A $OtherBackend listener PID is still active. Stop or verify that process before starting the $Backend listener."
}
$Existing = Get-TrackedListener -Paths $Paths -Backend $Backend
if ($Existing) {
    if (-not $Existing.Verified) { throw 'Listener PID points to an unverified process; it was left untouched.' }
    if ($Backend -eq 'api' -and $Existing.Run.config -ne $ResolvedConfig) {
        throw 'The API listener is running with a different configuration; stop it before changing configurations.'
    }
    if ($Backend -eq 'api' -and -not $Recovery) { Set-RecoveryIntent $Root 'api' $true @{ ConfigPath = $ResolvedConfig } }
    Write-Host "Zhuorui $Backend listener is already running with PID $($Existing.Pid)."
    exit 0
}
if ($Backend -eq 'api' -and -not $Recovery) { Set-RecoveryIntent $Root 'api' $true @{ ConfigPath = $ResolvedConfig } }
Remove-Item -LiteralPath $Paths.Pid -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Paths.Current -Force -ErrorAction SilentlyContinue
$StopFile = $null
$StateFile = $null
$LiveOrdersEnabled = $false
if ($Backend -eq 'api') {
    $LiveOrdersEnabled = Get-ApiListenerOrderMode -Config $Config
    $PythonExe = Get-ApiListenerPython -ProjectRoot $Root -ConfigDirectory $ConfigDirectory -ApiConfig $Config.api
    $Mode = 'listen'
    $StopFile = if ($Config.api.stop_file) {
        Resolve-ListenerConfigPath -Value ([string]$Config.api.stop_file) -BaseDirectory $ConfigDirectory
    } else { Join-Path $Root 'runtime\api\listener.stop' }
    $StateFile = if ($Config.api.state_file) {
        Resolve-ListenerConfigPath -Value ([string]$Config.api.state_file) -BaseDirectory $ConfigDirectory
    } else { Join-Path $Root 'runtime\api\listener-state.json' }
} else {
    $Mode = 'server'
    $PythonExe = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw 'UI Python was not found. Install requirements.txt in .venv.'
    }
}
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$RunStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmssfff'Z'")
$OutLog = Join-Path $LogDir "$($Paths.Name)_$RunStamp.out.log"
$ErrLog = Join-Path $LogDir "$($Paths.Name)_$RunStamp.err.log"
# Some hosts expose both Path and PATH; Start-Process rejects that duplication.
$ProcessPathValue = $env:Path
[System.Environment]::SetEnvironmentVariable('PATH', $null, [System.EnvironmentVariableTarget]::Process)
[System.Environment]::SetEnvironmentVariable('Path', $ProcessPathValue, [System.EnvironmentVariableTarget]::Process)
$Process = Start-Process -WindowStyle Hidden -WorkingDirectory $Root -FilePath $PythonExe `
    -ArgumentList @('-u', "`"$($Paths.Script)`"", $Mode, '--config', "`"$ResolvedConfig`"") `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog -PassThru
$Process.Id | Set-Content -LiteralPath $Paths.Pid -Encoding ascii
[ordered]@{
    pid = $Process.Id
    backend = $Backend
    script = $Paths.Script
    python = $PythonExe
    started_utc = $Process.StartTime.ToUniversalTime().ToString('o')
    stdout = $OutLog
    stderr = $ErrLog
    config = $ResolvedConfig
    stop_file = $StopFile
    state_file = $StateFile
    live_orders_enabled = $LiveOrdersEnabled
} | ConvertTo-Json | Set-Content -LiteralPath $Paths.Current -Encoding utf8
Start-Sleep -Milliseconds 1000
$Process.Refresh()
if ($Process.HasExited) {
    Remove-Item -LiteralPath $Paths.Pid, $Paths.Current -Force -ErrorAction SilentlyContinue
    throw "Zhuorui $Backend listener exited during startup. See $ErrLog"
}
Write-Host "Started Zhuorui $Backend listener with PID $($Process.Id)."
Write-Host "stdout: $OutLog"
Write-Host "stderr: $ErrLog"
} finally { $ControlLock.Dispose() }
