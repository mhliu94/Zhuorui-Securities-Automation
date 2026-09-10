param(
    [int]$Port = 443,
    [string]$HostAddress = "localhost"
)

# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\check_zhuorui_monitor.ps1") @PSBoundParameters @args
