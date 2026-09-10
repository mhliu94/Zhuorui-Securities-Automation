param(
    [int]$Port = 443,
    [int]$RedirectHttpPort = 80,
    [string]$HostAddress = "0.0.0.0",
    [string]$PublicHost,
    [ValidateRange(10, 86400)]
    [int]$Interval = 60,
    [string]$CertificatePath,
    [string]$PrivateKeyPath,
    [switch]$OpenBrowser
)

# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\start_zhuorui_monitor.ps1") @PSBoundParameters @args
