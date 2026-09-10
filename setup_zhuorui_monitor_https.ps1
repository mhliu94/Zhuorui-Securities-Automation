param(
    [string]$CommonName = $env:COMPUTERNAME,
    [string[]]$AdditionalDnsName = @(),
    [string[]]$AdditionalIpAddress = @(),
    [switch]$Force
)

# Compatibility launcher; implementation is under scripts/windows.
& (Join-Path $PSScriptRoot "scripts\windows\setup_zhuorui_monitor_https.ps1") @PSBoundParameters @args
