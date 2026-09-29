$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'recovery_common.ps1')
if (-not (Get-RecoveryPython $Root)) { throw 'Prepare the independent Python runtime first; see docs/windows-recovery.md.' }

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$PowerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Script = Join-Path $PSScriptRoot 'desktop_zhuorui_monitor.ps1'
$Hash = [Security.Cryptography.SHA256]::Create()
try { $Suffix = ([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant())))).Replace('-', '').Substring(0, 12) }
finally { $Hash.Dispose() }
$Principal = New-ScheduledTaskPrincipal -UserId $Identity.Name -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Desktop = [Environment]::GetFolderPath('Desktop')
$Shell = New-Object -ComObject WScript.Shell
$Controls = @(
    @{ Action = 'Open'; Name = "Zhuorui Monitor $Suffix"; Shortcut = 'Zhuorui Monitor.lnk'; Description = 'Start the Control Room if needed and open it in your browser.' },
    @{ Action = 'Restart'; Name = "Zhuorui Monitor Restart $Suffix"; Shortcut = 'Restart Zhuorui Monitor.lnk'; Description = 'Gracefully restart the Control Room and open it in your browser.' },
    @{ Action = 'StartApi'; Name = "Zhuorui API $Suffix"; Shortcut = $null; Description = 'Start the Zhuorui API listener independently of Codex.' }
)
foreach ($Control in $Controls) {
    $Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action {1}' -f $Script, $Control.Action
    $Existing = Get-ScheduledTask -TaskName $Control.Name -ErrorAction SilentlyContinue
    if ($Existing -and ($Existing.Actions.Execute -ne $PowerShell -or $Existing.Actions.Arguments -ne $Arguments)) {
        throw "An unrelated task has the expected name: $($Control.Name). It was left untouched."
    }
    $TaskAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments -WorkingDirectory $Root
    $Definition = New-ScheduledTask -Action $TaskAction -Settings $Settings -Principal $Principal -Description $Control.Description
    $null = Register-ScheduledTask -TaskName $Control.Name -InputObject $Definition -Force
    if ($Control.Shortcut) {
        $ShortcutPath = Join-Path $Desktop $Control.Shortcut
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = $PowerShell
        $Shortcut.Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -Command "Start-ScheduledTask -TaskName ''{0}''"' -f $Control.Name
        $Shortcut.WorkingDirectory = $Root
        $Shortcut.WindowStyle = 7
        $Shortcut.IconLocation = (Join-Path $env:WINDIR 'System32\shell32.dll') + ',17'
        $Shortcut.Description = $Control.Description
        $Shortcut.Save()
        Write-Host "Created $ShortcutPath"
    }
}
Write-RecoveryJson (Join-Path (Get-RecoveryDirectory $Root) 'desktop-settings.json') @{
    version = 1; user = $Identity.Name; installed_at = [datetime]::UtcNow.ToString('o')
    monitor_task = $Controls[0].Name; monitor_restart_task = $Controls[1].Name; api_task = $Controls[2].Name
}
Write-Host 'Desktop controls use Windows Task Scheduler and project-owned Python. No Codex session is required.'
