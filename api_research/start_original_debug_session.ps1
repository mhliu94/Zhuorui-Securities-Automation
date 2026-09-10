param([string]$ConfigPath, [switch]$Visible)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath -VerifyBoot
$PrivateRoot = $Settings.private_dir
$SessionPath = Join-Path $PrivateRoot 'original-capture-session.json'
$Session = Get-Content -Raw -LiteralPath $SessionPath | ConvertFrom-Json
$SdkRoot = $Settings.sdk_root
if (-not $Session.backup_verified -or $Session.avd -ne $Settings.avd -or $Session.device -ne $Settings.device) {
    throw 'Original capture session has no verified backup.'
}
$BootEvidence = Get-Content -Raw -LiteralPath (Join-Path $PrivateRoot 'debug-ramdisk-evidence.json') | ConvertFrom-Json
$Ramdisk = $Settings.debug_ramdisk_file
if (-not $BootEvidence.boot_test_passed -or (Get-FileHash -LiteralPath $Ramdisk -Algorithm SHA256).Hash -ne $BootEvidence.output_sha256) {
    throw 'Temporary boot image has not passed or changed since the boot test.'
}
if ((& $Settings.adb devices) -match ([regex]::Escape($Settings.device) + '\s')) { throw 'Original emulator is already running.' }
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect emulator state.' }
$Trading = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw|cmd)(\.exe)?$' -and $_.CommandLine -match '(zhuorui_market_order\.(py|cmd)|start_zhuorui_listener\.ps1)' })
if ($Trading.Count -ne 0) { throw 'Trading listener is running.' }
$BootArgs = @('-avd',$Settings.avd,'-port',[string]$Settings.port,'-accel','auto','-no-snapshot','-ramdisk',$Ramdisk,'-show-kernel','-qemu','-append','androidboot.verifiedbootstate=orange')
Assert-CaptureBackup $Settings $Session
Assert-TradingStopped
$SavedAvdHome = $env:ANDROID_AVD_HOME
try {
$env:ANDROID_AVD_HOME = $Settings.avd_home
if (-not $Visible) { $BootArgs = @('-no-window') + $BootArgs }
$WindowStyle = if ($Visible) { 'Normal' } else { 'Hidden' }
$OriginalProcess = Start-Process -FilePath $Settings.emulator_executable -ArgumentList (ConvertTo-ProcessArguments $BootArgs) -WindowStyle $WindowStyle -PassThru `
    -RedirectStandardOutput (Join-Path $PrivateRoot 'original-capture-boot.out.log') -RedirectStandardError (Join-Path $PrivateRoot 'original-capture-boot.err.log')
$Session.phase = 'temporary_debug_boot'
$Session | Add-Member -NotePropertyName emulator_pid -NotePropertyValue $OriginalProcess.Id -Force
$Session | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $SessionPath -Encoding utf8
Write-Output 'Started the configured original AVD with a temporary debug ramdisk; existing app data and installed system images retained.'
} finally { $env:ANDROID_AVD_HOME = $SavedAvdHome }
