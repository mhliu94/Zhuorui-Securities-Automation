$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidPath = Join-Path $Root "zhuorui_listener.pid"
$CurrentRunPath = Join-Path $Root "zhuorui_listener.current.json"

if (-not (Test-Path $PidPath)) {
    Write-Host "Zhuorui listener is not running: no PID file found."
    exit 0
}

$PidValue = (Get-Content $PidPath -Raw).Trim()
if ($PidValue -notmatch '^\d+$') {
    Remove-Item -LiteralPath $PidPath -Force
    if (Test-Path $CurrentRunPath) {
        Remove-Item -LiteralPath $CurrentRunPath -Force
    }
    Write-Host "Removed invalid PID file."
    exit 0
}

$Process = Get-Process -Id ([int]$PidValue) -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item -LiteralPath $PidPath -Force
    if (Test-Path $CurrentRunPath) {
        Remove-Item -LiteralPath $CurrentRunPath -Force
    }
    Write-Host "Zhuorui listener was not running. Removed stale PID file."
    exit 0
}

Stop-Process -Id ([int]$PidValue) -Force
Remove-Item -LiteralPath $PidPath -Force
if (Test-Path $CurrentRunPath) {
    $CurrentRun = Get-Content $CurrentRunPath -Raw | ConvertFrom-Json
    Remove-Item -LiteralPath $CurrentRunPath -Force
    Write-Host "stdout: $($CurrentRun.stdout)"
    Write-Host "stderr: $($CurrentRun.stderr)"
}
Write-Host "Stopped Zhuorui listener with PID $PidValue."
