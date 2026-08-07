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

$CurrentRun = $null
$IsTrackedListener = $false
if (Test-Path $CurrentRunPath) {
    try {
        $CurrentRun = Get-Content $CurrentRunPath -Raw | ConvertFrom-Json
        $RecordedStart = ([datetime]$CurrentRun.started_utc).ToUniversalTime()
        $ActualStart = $Process.StartTime.ToUniversalTime()
        $IsTrackedListener = `
            ([int]$CurrentRun.pid -eq [int]$PidValue) -and `
            ([math]::Abs(($RecordedStart - $ActualStart).TotalSeconds) -le 30)
    } catch {
        $IsTrackedListener = $false
    }
}
if (-not $IsTrackedListener) {
    Remove-Item -LiteralPath $PidPath -Force
    Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
    Write-Host "Listener PID file was stale; process $PidValue was left untouched."
    exit 0
}

& taskkill.exe /PID $PidValue /T /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not stop Zhuorui listener process tree $PidValue."
}
Remove-Item -LiteralPath $PidPath -Force
if (Test-Path $CurrentRunPath) {
    Remove-Item -LiteralPath $CurrentRunPath -Force
    Write-Host "stdout: $($CurrentRun.stdout)"
    Write-Host "stderr: $($CurrentRun.stderr)"
}
Write-Host "Stopped Zhuorui listener with PID $PidValue."
