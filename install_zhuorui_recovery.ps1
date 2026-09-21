param([Management.Automation.PSCredential]$Credential, [switch]$InteractiveOnly)
& (Join-Path $PSScriptRoot 'scripts\windows\install_zhuorui_recovery.ps1') @PSBoundParameters
