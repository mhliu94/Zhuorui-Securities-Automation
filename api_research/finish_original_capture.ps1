param([string]$ConfigPath, [switch]$Visible)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath
$ResearchRoot = $PSScriptRoot
$PrivateRoot = $Settings.private_dir
$SessionPath = Join-Path $PrivateRoot 'original-capture-session.json'
$Session = Get-Content -Raw -LiteralPath $SessionPath | ConvertFrom-Json
Assert-CaptureBackup $Settings $Session
if ($Session.phase -eq 'restored') { Write-Output 'Original capture session is already restored.'; return }
if ($Session.device -ne $Settings.device -or $Session.avd -ne $Settings.avd -or -not $Session.backup_verified) {
    throw 'Unexpected original capture session.'
}
$Adb = $Settings.adb
$AvdName = (& $Adb -s $Settings.device emu avd name | Select-Object -First 1).Trim()
if ($AvdName -ne $Settings.avd) { throw 'Unexpected original emulator identity.' }
$Trading = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|cmd)(\.exe)?$' -and $_.CommandLine -match '(zhuorui_market_order\.(py|cmd)|start_zhuorui_listener\.ps1)' })
if ($Trading.Count -ne 0) { throw 'Stop the trading listener before the normal restart.' }
Assert-TradingStopped
& (Join-Path $ResearchRoot 'stop_capture.ps1') -ConfigPath $Settings.config_path
& $Adb -s $Settings.device emu kill
if ($LASTEXITCODE -ne 0) { throw 'Could not stop the original capture boot.' }
$ExitDeadline = [DateTime]::UtcNow.AddSeconds(45)
do {
    Start-Sleep -Milliseconds 500
    $OldProcess = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(emulator|qemu-system-.+)\.exe$' -and $_.CommandLine -match ('-avd"?\s+"?' + [regex]::Escape($Settings.avd) + '"?(?:\s|$)') })
    $Online = (& $Adb devices) -match ([regex]::Escape($Settings.device) + '\s')
    if (-not $OldProcess -and -not $Online) { break }
} while ([DateTime]::UtcNow -lt $ExitDeadline)
if ($OldProcess -or $Online) { throw 'Original capture emulator has not exited.' }
$SdkRoot = $Settings.sdk_root
Assert-CaptureBackup $Settings $Session
$SavedAvdHome = $env:ANDROID_AVD_HOME
try {
    $env:ANDROID_AVD_HOME = $Settings.avd_home
    $BootArgs = @('-avd', $Settings.avd, '-port', [string]$Settings.port, '-accel', 'auto', '-no-snapshot-load')
    if (-not $Visible) { $BootArgs += '-no-window' }
    $WindowStyle = if ($Visible) { 'Normal' } else { 'Hidden' }
    Start-Process -FilePath $Settings.emulator_executable -ArgumentList (ConvertTo-ProcessArguments $BootArgs) -WindowStyle $WindowStyle `
        -RedirectStandardOutput (Join-Path $PrivateRoot 'original-restored.out.log') -RedirectStandardError (Join-Path $PrivateRoot 'original-restored.err.log') | Out-Null
} finally { $env:ANDROID_AVD_HOME = $SavedAvdHome }
$BootDeadline = [DateTime]::UtcNow.AddSeconds($Settings.boot_timeout_seconds)
$Booted = $false
do {
    Start-Sleep -Seconds 2
    $Devices = & $Adb devices
    if ($Devices -match ([regex]::Escape($Settings.device) + '\s+device')) {
        $Booted = ((& $Adb -s $Settings.device shell getprop sys.boot_completed) -eq '1')
        if ($Booted) { break }
    }
} while ([DateTime]::UtcNow -lt $BootDeadline)
if (-not $Booted) { throw 'Normal boot did not complete; inspect the local restore logs.' }
$Fingerprint = (& $Adb -s $Settings.device shell getprop ro.build.fingerprint).Trim()
$AndroidId = (& $Adb -s $Settings.device shell settings get secure android_id).Trim()
$Debuggable = (& $Adb -s $Settings.device shell getprop ro.debuggable).Trim()
if ($Fingerprint -ne $Session.fingerprint -or $AndroidId -ne $Session.android_id -or $Debuggable -ne '0') {
    throw 'Normal boot identity/debugging verification failed.'
}
& ($Settings.python_executable) (Join-Path $ResearchRoot 'restore_original_proxy.py') --config $Settings.config_path
if ($LASTEXITCODE -ne 0) { throw 'Final proxy verification failed.' }
& $Adb -s $Settings.device shell am start -n com.zhuorui.securities/.ui.SplashActivity | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not reopen the existing app.' }
$Session.phase = 'restored'
$Session | Add-Member -NotePropertyName restored_utc -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
$Session | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $SessionPath -Encoding utf8
Write-Output 'Original emulator restored to its normal boot, with its identity and saved proxy settings verified. Trading listener remains stopped.'
