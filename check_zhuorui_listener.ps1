$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidPath = Join-Path $Root "zhuorui_listener.pid"
$CurrentRunPath = Join-Path $Root "zhuorui_listener.current.json"
$OutLog = $null
$ErrLog = $null

if (Test-Path $CurrentRunPath) {
    $CurrentRun = Get-Content $CurrentRunPath -Raw | ConvertFrom-Json
    $OutLog = [string]$CurrentRun.stdout
    $ErrLog = [string]$CurrentRun.stderr
}

if (-not (Test-Path $PidPath)) {
    Write-Host "Zhuorui listener is not running: no PID file found."
    exit 1
}

$PidValue = (Get-Content $PidPath -Raw).Trim()
if ($PidValue -notmatch '^\d+$') {
    Write-Host "Zhuorui listener status is unknown: PID file is invalid."
    exit 1
}

$Process = Get-Process -Id ([int]$PidValue) -ErrorAction SilentlyContinue
if (-not $Process) {
    Write-Host "Zhuorui listener is not running. Stale PID file: $PidValue"
    exit 1
}

Write-Host "Zhuorui listener is running with PID $PidValue."
if ($OutLog) {
    Write-Host "stdout: $OutLog"
}
if ($ErrLog) {
    Write-Host "stderr: $ErrLog"
}

if ($OutLog -and (Test-Path $OutLog)) {
    Write-Host ""
    Write-Host "Recent stdout:"
    Get-Content $OutLog -Tail 10
}

if ($ErrLog -and (Test-Path $ErrLog)) {
    Write-Host ""
    Write-Host "Recent stderr:"
    Get-Content $ErrLog -Tail 10
}
