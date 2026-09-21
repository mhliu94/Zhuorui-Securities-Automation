$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'watchdog_core.ps1')
try { Invoke-RecoveryTick $Root }
catch {
    Write-RecoveryEvent $Root 'watchdog' 'check_failed' 'Watchdog could not finish its check; verify settings, locks, and Windows task history.'
    exit 1
}
