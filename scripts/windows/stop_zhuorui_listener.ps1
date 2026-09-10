param([ValidateSet('api', 'ui')][string]$Backend = 'api')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'listener_common.ps1')
$Paths = Get-ListenerPaths -ProjectRoot $Root -Backend $Backend
$Tracked = Get-TrackedListener -Paths $Paths -Backend $Backend
if (-not $Tracked) {
    Remove-Item -LiteralPath $Paths.Pid, $Paths.Current -Force -ErrorAction SilentlyContinue
    Write-Host "Zhuorui $Backend listener is not running."
    exit 0
}
if (-not $Tracked.Verified) {
    throw 'Listener PID points to an unverified process; it was left untouched.'
}
if ($Backend -eq 'api') {
    $StopFile = [string]$Tracked.Run.stop_file
    if (-not $StopFile -or -not [System.IO.Path]::IsPathRooted($StopFile)) {
        throw 'The API stop request path is missing or invalid; process was left running.'
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $StopFile) -Force | Out-Null
    'stop' | Set-Content -LiteralPath $StopFile -Encoding ascii
    if (-not $Tracked.Process.WaitForExit(30000)) {
        throw 'API listener is still finishing work after 30 seconds. Stop remains requested; process was not force-killed.'
    }
} else {
    & taskkill.exe /PID $Tracked.Pid /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stop UI listener process tree $($Tracked.Pid)." }
}
Remove-Item -LiteralPath $Paths.Pid, $Paths.Current -Force -ErrorAction SilentlyContinue
Write-Host "Stopped Zhuorui $Backend listener with PID $($Tracked.Pid)."
