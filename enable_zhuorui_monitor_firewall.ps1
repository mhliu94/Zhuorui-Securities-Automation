param(
    [ValidateRange(1, 65535)]
    [int]$HttpsPort = 443,
    [ValidateRange(1, 65535)]
    [int]$HttpRedirectPort = 80
)

# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\enable_zhuorui_monitor_firewall.ps1") @PSBoundParameters @args
