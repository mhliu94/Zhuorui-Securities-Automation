param([ValidateSet('api', 'ui')][string]$Backend = 'api', [switch]$Recovery,
      [string]$IntentRevision, [int]$ExpectedPid, [string]$ExpectedStartedUtc)
# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\stop_zhuorui_listener.ps1") @PSBoundParameters @args
