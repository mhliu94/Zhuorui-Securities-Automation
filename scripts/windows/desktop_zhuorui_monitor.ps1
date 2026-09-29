param([ValidateSet('Open', 'Restart', 'StartApi')][string]$Action = 'Open')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $PSScriptRoot 'recovery_common.ps1')
$PowerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$LogDirectory = Join-Path $Root 'logs'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$LogPath = Join-Path $LogDirectory ('desktop_control_{0}.log' -f (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ'))
$ControlLock = $null
Start-Transcript -Path $LogPath | Out-Null

function Invoke-DesktopLauncher {
    param([string]$Name, [string[]]$LauncherArguments = @())
    # Isolate launchers that use exit, so the desktop controller can finish.
    & $PowerShell -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $Name) @LauncherArguments
    if ($LASTEXITCODE -ne 0) { throw "$Name failed. See $LogPath" }
}

try {
    $ControlLock = Enter-ServiceControlLock $Root 'desktop' 120
    $Python = Get-RecoveryPython $Root
    if (-not $Python) { throw 'Prepare the independent Python runtime before using desktop controls.' }
    & $Python (Join-Path $PSScriptRoot 'validate_recovery_context.py')
    if ($LASTEXITCODE -ne 0) { throw 'The desktop task must run outside Codex under the Windows account that owns the API session.' }

    if ($Action -eq 'StartApi') {
        Invoke-DesktopLauncher 'start_zhuorui_listener.ps1' @('-Backend', 'api')
    } else {
        # Preserve ports, public host, certificates, and refresh interval on restart.
        $Intent = Read-RecoveryIntent $Root 'monitor'
        $Parameters = if ($Intent) { $Intent.parameters } else { $null }
        $Arguments = @()
        $Allowed = @('Port', 'RedirectHttpPort', 'HostAddress', 'PublicHost', 'Interval', 'CertificatePath', 'PrivateKeyPath')
        if ($Parameters) {
            foreach ($Property in $Parameters.PSObject.Properties) {
                if ($Property.Name -notin $Allowed) { throw 'Unsupported saved monitor launch parameter.' }
                $Arguments += ('-' + $Property.Name)
                $Arguments += [string]$Property.Value
            }
        }
        if ($Action -eq 'Restart') { Invoke-DesktopLauncher 'stop_zhuorui_monitor.ps1' }
        $Arguments += '-OpenBrowser'
        Invoke-DesktopLauncher 'start_zhuorui_monitor.ps1' $Arguments
    }
} catch {
    Write-Output $_.Exception.Message
    $Popup = New-Object -ComObject WScript.Shell
    $null = $Popup.Popup("Zhuorui desktop control failed. See:`n$LogPath", 60, 'Zhuorui Control Room', 16)
    exit 1
} finally {
    if ($ControlLock) { $ControlLock.Dispose() }
    Stop-Transcript | Out-Null
}
