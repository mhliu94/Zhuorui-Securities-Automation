param([switch]$Recovery, [string]$IntentRevision, [int]$ExpectedPid, [string]$ExpectedStartedUtc)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'recovery_common.ps1')
$ControlLock = Enter-ServiceControlLock $Root 'monitor'
try {
if ($Recovery) {
    if (-not $IntentRevision) { throw 'Recovery stop requires a current intent revision.' }
    $Intent = Read-RecoveryIntent $Root 'monitor'
    if (-not $Intent -or $Intent.revision -ne $IntentRevision) { return }
}
if (-not $Recovery) { Set-RecoveryIntent $Root 'monitor' $false }
$PidPath = Join-Path $Root "zhuorui_monitor.pid"
$CurrentRunPath = Join-Path $Root "zhuorui_monitor.current.json"
$TrackedMonitor = Get-TrackedMonitor $Root
if ($TrackedMonitor -and -not $TrackedMonitor.Verified) { throw 'Monitor PID points to an unverified process; it was left untouched.' }
if ($Recovery -and $TrackedMonitor -and ($TrackedMonitor.Pid -ne $ExpectedPid -or
    $TrackedMonitor.Process.StartTime.ToUniversalTime().ToString('o') -ne $ExpectedStartedUtc)) { return }

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

if ($RunInfo.stop_file) {
    $StopPath = Join-Path $Root 'runtime\monitor.stop'
    if ($RunInfo.stop_file -ne $StopPath) { throw 'Monitor stop request path is invalid.' }
    'stop' | Set-Content -LiteralPath $StopPath -Encoding ascii
    if (-not $Process.WaitForExit(30000)) { throw 'Control Room is still stopping; process was not force-killed.' }
} elseif ($Recovery) {
    throw 'This older monitor requires one manual restart to support graceful recovery.'
} else {
    & taskkill.exe /PID $PidValue /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stop Zhuorui Control Room process tree $PidValue." }
}
Remove-Item -LiteralPath $PidPath -Force
Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
Write-Host "Stopped Zhuorui Control Room with PID $PidValue."
} finally { $ControlLock.Dispose() }
