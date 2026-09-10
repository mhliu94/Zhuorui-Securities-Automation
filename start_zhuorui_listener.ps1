param(
    [string]$ConfigPath = ".\zhuorui_config.json",
    [ValidateSet('api', 'ui')][string]$Backend = 'api'
)

# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\start_zhuorui_listener.ps1") @PSBoundParameters @args
