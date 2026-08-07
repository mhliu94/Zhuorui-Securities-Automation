$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidPath = Join-Path $Root "zhuorui_monitor.pid"
$CurrentRunPath = Join-Path $Root "zhuorui_monitor.current.json"

if (-not (Test-Path -LiteralPath $PidPath)) {
    Write-Host "Zhuorui Control Room is not running: no PID file found."
    exit 0
}

$PidValue = (Get-Content -LiteralPath $PidPath -Raw).Trim()
if ($PidValue -notmatch '^\d+$') {
    Remove-Item -LiteralPath $PidPath -Force
    Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
    Write-Host "Removed invalid monitor PID file."
    exit 0
}

$Process = Get-Process -Id ([int]$PidValue) -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item -LiteralPath $PidPath -Force
    Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
    Write-Host "Zhuorui Control Room was not running. Removed stale PID file."
    exit 0
}

$IsTrackedMonitor = $false
if (Test-Path -LiteralPath $CurrentRunPath) {
    try {
        $RunInfo = Get-Content -LiteralPath $CurrentRunPath -Raw | ConvertFrom-Json
        $RecordedStart = ([datetime]$RunInfo.started_utc).ToUniversalTime()
        $ActualStart = $Process.StartTime.ToUniversalTime()
        $IsTrackedMonitor = `
            ([int]$RunInfo.pid -eq [int]$PidValue) -and `
            ([math]::Abs(($RecordedStart - $ActualStart).TotalSeconds) -le 30)
    } catch {
        $IsTrackedMonitor = $false
    }
}
if (-not $IsTrackedMonitor) {
    Remove-Item -LiteralPath $PidPath -Force
    Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
    Write-Host "Monitor PID file was stale; process $PidValue was left untouched."
    exit 0
}

& taskkill.exe /PID $PidValue /T /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not stop Zhuorui Control Room process tree $PidValue."
}
Remove-Item -LiteralPath $PidPath -Force
Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
Write-Host "Stopped Zhuorui Control Room with PID $PidValue."
