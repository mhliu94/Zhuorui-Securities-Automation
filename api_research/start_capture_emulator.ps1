param([switch]$Visible, [string]$ConfigPath)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath
$ResearchRoot = $PSScriptRoot
$ProjectRoot = Split-Path -Parent $ResearchRoot
$Devices = & $Settings.adb devices
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect emulator state.' }
if ($Devices -match ([regex]::Escape($Settings.test_device) + '\s')) {
    throw 'Configured test port already has an emulator. Inspect it before starting another.'
}
$SdkRoot = $Settings.sdk_root
$PrivateRoot = $Settings.private_dir
$TestAvd = Initialize-TestAvd $Settings capture
$AvdRoot = $TestAvd.avd_home
$OriginalAvdHome = $env:ANDROID_AVD_HOME
try {
    $env:ANDROID_AVD_HOME = $AvdRoot
    $EmulatorArguments = @('-avd',$Settings.test_avd,'-port',[string]$Settings.test_port,'-no-snapshot','-no-boot-anim','-no-audio','-accel','on','-gpu','software')
    if (-not $Visible) { $EmulatorArguments += '-no-window' }
    $CaptureWindowStyle = if ($Visible) { 'Normal' } else { 'Hidden' }
    $Process = Start-Process -FilePath $Settings.emulator_executable `
        -ArgumentList (ConvertTo-ProcessArguments $EmulatorArguments) `
        -WorkingDirectory $ResearchRoot -WindowStyle $CaptureWindowStyle -PassThru `
        -RedirectStandardOutput (Join-Path $PrivateRoot 'capture-emulator.out.log') `
        -RedirectStandardError (Join-Path $PrivateRoot 'capture-emulator.err.log')
    @{ pid=$Process.Id; device=$Settings.test_device; started_utc=[DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PrivateRoot 'capture-emulator.json')
    Write-Output "Started separate capture emulator; PID $($Process.Id), configured test port."
} finally {
    $env:ANDROID_AVD_HOME = $OriginalAvdHome
}
