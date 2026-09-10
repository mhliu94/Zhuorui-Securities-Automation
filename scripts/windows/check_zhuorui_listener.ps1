param([ValidateSet('api', 'ui')][string]$Backend = 'api')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'listener_common.ps1')
$Paths = Get-ListenerPaths -ProjectRoot $Root -Backend $Backend
$Tracked = Get-TrackedListener -Paths $Paths -Backend $Backend
if (-not $Tracked -or -not $Tracked.Verified) {
    Write-Host "Zhuorui $Backend listener is stopped or its process identity is unverified."
    exit 1
}
Write-Host "Zhuorui $Backend listener is running with PID $($Tracked.Pid)."
if ($Backend -eq 'api') {
    Write-Host "Live order execution enabled: $($Tracked.Run.live_orders_enabled -eq $true)"
}
Write-Host "stdout: $($Tracked.Run.stdout)"
Write-Host "stderr: $($Tracked.Run.stderr)"
