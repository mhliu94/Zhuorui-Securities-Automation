param([Management.Automation.PSCredential]$Credential, [switch]$InteractiveOnly)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'recovery_common.ps1')
. (Join-Path $PSScriptRoot 'windows_credential.ps1')
$Python = Get-RecoveryPython $Root
if (-not $Python) { throw 'Prepare the independent runtime before installing recovery.' }
foreach ($Component in @('api','monitor')) {
    if (-not (Read-RecoveryIntent $Root $Component)) { throw 'Use the updated Start or Stop controls once for each component before installing recovery.' }
}
# This account owns the encrypted broker session. Never silently switch to SYSTEM/S4U.
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$UserName = $Identity.Name
$LogonType = if ($InteractiveOnly) { 'Interactive' } else { 'Password' }
if (-not $InteractiveOnly) {
    if (-not $Credential) { $Credential = Get-ZhuoruiWindowsCredential $UserName }
    $CredentialSid = (New-Object Security.Principal.NTAccount($Credential.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($CredentialSid -ne $Identity.User.Value) { throw 'Use the Windows account that owns this project session.' }
}
$Hash = [Security.Cryptography.SHA256]::Create()
try { $Suffix = ([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant())))).Replace('-','').Substring(0,12) }
finally { $Hash.Dispose() }
$TaskName = "Zhuorui Recovery $Suffix"
$PowerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $PSScriptRoot 'watch_zhuorui_services.ps1')
$Action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $Root
$Boot = New-ScheduledTaskTrigger -AtStartup
$Boot.Delay = 'PT45S'
$Repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$Triggers = if ($InteractiveOnly) { @((New-ScheduledTaskTrigger -AtLogOn -User $UserName), $Repeat) } else { @($Boot, $Repeat) }
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId $UserName -LogonType $LogonType -RunLevel Limited
$Definition = New-ScheduledTask -Action $Action -Trigger $Triggers -Settings $Settings -Principal $Principal -Description "Recover this project's API listener and monitor while preserving intentional Stop settings."
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing -and ($Existing.Actions.Execute -ne $PowerShell -or $Existing.Actions.Arguments -ne $Arguments)) {
    throw 'An unrelated task has the expected name; it was left untouched.'
}
# Validate the real scheduled logon (including DPAPI) before enabling trading recovery.
$ProbeName = "$TaskName validation"
if (Get-ScheduledTask -TaskName $ProbeName -ErrorAction SilentlyContinue) { throw 'A context validation task already exists; inspect it before retrying.' }
$ProbeFile = Join-Path (Get-RecoveryDirectory $Root) 'context-validation.json'
Remove-Item -LiteralPath $ProbeFile -Force -ErrorAction SilentlyContinue
$ProbeAction = New-ScheduledTaskAction -Execute $Python -Argument ('"{0}"' -f (Join-Path $PSScriptRoot 'validate_recovery_context.py')) -WorkingDirectory $Root
$ProbeDefinition = New-ScheduledTask -Action $ProbeAction -Settings $Settings -Principal $Principal
$ProbeRegistered = $false
try {
    if ($InteractiveOnly) { $null = Register-ScheduledTask -TaskName $ProbeName -InputObject $ProbeDefinition }
    else {
        $PlainPassword = $Credential.GetNetworkCredential().Password
        try { $null = Register-ScheduledTask -TaskName $ProbeName -InputObject $ProbeDefinition -User $Credential.UserName -Password $PlainPassword }
        finally { $PlainPassword = $null }
    }
    $ProbeRegistered = $true
    Start-ScheduledTask -TaskName $ProbeName
    $Deadline = [datetime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $ProbeFile) { break }
    } while ([datetime]::UtcNow -lt $Deadline)
    if (-not (Test-Path -LiteralPath $ProbeFile)) { throw 'Scheduled account validation did not finish. Check the Windows task history.' }
    $ProbeResult = Get-Content -LiteralPath $ProbeFile -Raw | ConvertFrom-Json
    if ($ProbeResult.ok -ne $true) { throw 'Scheduled account validation failed. Recovery was not enabled; inspect context-validation.json.' }
} finally {
    if ($ProbeRegistered) {
        Stop-ScheduledTask -TaskName $ProbeName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $ProbeName -Confirm:$false
    }
}
if ($InteractiveOnly) {
    $null = Register-ScheduledTask -TaskName $TaskName -InputObject $Definition -Force
} else {
    $PlainPassword = $Credential.GetNetworkCredential().Password
    try { $null = Register-ScheduledTask -TaskName $TaskName -InputObject $Definition -User $Credential.UserName -Password $PlainPassword -Force }
    finally { $PlainPassword = $null; $Credential = $null }
}
Write-RecoveryJson (Join-Path (Get-RecoveryDirectory $Root) 'settings.json') @{
    version=1; enabled=$true; task_name=$TaskName; logon_type=$LogonType; installed_at=[datetime]::UtcNow.ToString('o')
}
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed $TaskName ($LogonType). Checking every minute."
