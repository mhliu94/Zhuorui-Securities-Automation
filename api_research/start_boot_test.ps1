param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath
$PrivateRoot = $Settings.private_dir
$SdkRoot = $Settings.sdk_root
$Devices = & $Settings.adb devices
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect emulator state.' }
if ($Devices -match ([regex]::Escape($Settings.boot_test_device) + '\s')) { throw 'The boot-test port is already occupied.' }
$TestAvd = Initialize-TestAvd $Settings boot
$AvdRoot = $TestAvd.avd_home
$SavedAvdHome = $env:ANDROID_AVD_HOME
try {
    $env:ANDROID_AVD_HOME = $AvdRoot
    $BootArgs = @('-avd',$Settings.boot_test_avd,'-port',[string]$Settings.boot_test_port,'-no-snapshot','-no-window','-no-boot-anim','-no-audio','-accel','on','-gpu','software',
        '-ramdisk',$Settings.debug_ramdisk_file,'-show-kernel','-qemu','-append','androidboot.verifiedbootstate=orange')
    $TestProcess = Start-Process -FilePath $Settings.emulator_executable -ArgumentList (ConvertTo-ProcessArguments $BootArgs) -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $PrivateRoot 'boot-test.out.log') -RedirectStandardError (Join-Path $PrivateRoot 'boot-test.err.log')
    @{pid=$TestProcess.Id;device=$Settings.boot_test_device;avd=$Settings.boot_test_avd} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PrivateRoot 'boot-test.json')
    Write-Output 'Started the empty Google Play boot test on the configured boot-test port; original emulator unchanged.'
} finally {
    $env:ANDROID_AVD_HOME = $SavedAvdHome
}
