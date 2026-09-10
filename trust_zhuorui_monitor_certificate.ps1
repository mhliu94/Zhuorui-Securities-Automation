param(
    [string]$CertificatePath,
    [ValidateSet("LocalMachine", "CurrentUser")]
    [string]$StoreScope = "LocalMachine"
)

# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\trust_zhuorui_monitor_certificate.ps1") @PSBoundParameters @args
