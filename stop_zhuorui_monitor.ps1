param([switch]$Recovery, [string]$IntentRevision, [int]$ExpectedPid, [string]$ExpectedStartedUtc)
# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\stop_zhuorui_monitor.ps1") @PSBoundParameters @args
