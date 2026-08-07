param(
    [ValidateRange(1, 65535)]
    [int]$HttpsPort = 443,
    [ValidateRange(1, 65535)]
    [int]$HttpRedirectPort = 80
)

$ErrorActionPreference = "Stop"
$RuleName = "Zhuorui Control Room HTTPS"
$Ports = @($HttpRedirectPort, $HttpsPort) | Select-Object -Unique
$Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated PowerShell window."
}

$Existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($Existing) {
    $Existing | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Any | Out-Null
    $Existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Ports | Out-Null
    Write-Host "Updated Windows Firewall rule '$RuleName' for TCP ports $($Ports -join ', ')."
} else {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Description "Inbound HTTPS access to the authenticated Zhuorui Control Room." `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Ports `
        -Profile Any `
        -Enabled True | Out-Null
    Write-Host "Created Windows Firewall rule '$RuleName' for TCP ports $($Ports -join ', ')."
}
