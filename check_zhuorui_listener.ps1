param([ValidateSet('api', 'ui')][string]$Backend = 'api')
# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\check_zhuorui_listener.ps1") @PSBoundParameters @args
