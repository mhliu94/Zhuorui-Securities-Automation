param([switch]$Visible, [string]$ConfigPath)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath
$ResearchRoot = $PSScriptRoot
$AdbPath = $Settings.adb
$CapturePython = $Settings.python_executable

function Invoke-CaptureAdb {
    param([string[]]$AdbArguments)
    $Output = & $AdbPath -s $Settings.test_device @AdbArguments
    if ($LASTEXITCODE -ne 0) { throw 'A test-emulator setup command failed.' }
    return $Output
}

function Wait-CaptureBoot {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Settings.boot_timeout_seconds)
    do {
        $Devices = & $AdbPath devices
        if ($Devices -match ([regex]::Escape($Settings.test_device) + '\s+device')) {
            $Boot = (Invoke-CaptureAdb -AdbArguments @('shell','getprop','sys.boot_completed') | Out-String).Trim()
            if ($Boot -eq '1') { return }
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $Deadline)
    throw 'The separate capture emulator did not finish booting in the configured timeout.'
}

if (-not (Test-Path -LiteralPath $CapturePython)) { throw 'The research Python environment is missing.' }
if (Get-NetTCPConnection -LocalPort $Settings.proxy_port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Configured capture port is in use. Inspect the existing session first.'
}
$StartedCapture = $false
try {
    & (Join-Path $ResearchRoot 'start_capture_emulator.ps1') -ConfigPath $Settings.config_path -Visible:$Visible
    $StartedCapture = $true
    Wait-CaptureBoot
    $AvdName = (Invoke-CaptureAdb -AdbArguments @('emu','avd','name') | Select-Object -First 1).Trim()
    if ($AvdName -ne $Settings.test_avd) { throw 'Unexpected test emulator identity.' }
    Invoke-CaptureAdb -AdbArguments @('root') | Out-Null
    # ADB disconnects briefly when the debug daemon restarts as root.
    $RootDeadline = [DateTime]::UtcNow.AddSeconds(30)
    $IsRoot = $false
    do {
        Start-Sleep -Milliseconds 500
        $Devices = & $AdbPath devices
        if ($Devices -match ([regex]::Escape($Settings.test_device) + '\s+device')) {
            $UserId = (Invoke-CaptureAdb -AdbArguments @('shell','id','-u') | Out-String).Trim()
            if ($UserId -eq '0') { $IsRoot = $true; break }
        }
    } while ([DateTime]::UtcNow -lt $RootDeadline)
    if (-not $IsRoot) { throw 'The test emulator did not enable root debugging.' }

    & (Join-Path $ResearchRoot 'start_api_proxy.ps1') -ConfigPath $Settings.config_path
    $ProxyDeadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $Ready = Get-NetTCPConnection -LocalPort $Settings.proxy_port -State Listen -ErrorAction SilentlyContinue
        $CertificateReady = Test-Path -LiteralPath (Join-Path $Settings.private_dir 'mitmproxy\mitmproxy-ca-cert.pem')
        if ($Ready -and $CertificateReady) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $ProxyDeadline)
    if (-not $Ready -or -not $CertificateReady) { throw 'The local capture proxy did not start.' }
    & $CapturePython (Join-Path $ResearchRoot 'trust_capture_ca.py') --config $Settings.config_path
    if ($LASTEXITCODE -ne 0) { throw 'The capture certificate could not be mounted.' }
    $Installed = Invoke-CaptureAdb -AdbArguments @('shell','pm','path','com.zhuorui.securities')
    if (-not ($Installed -match '^package:')) { throw 'The copied Zhuorui APK is missing from the test emulator.' }
    Invoke-CaptureAdb -AdbArguments @('shell','settings','put','global','http_proxy',$Settings.emulator_proxy) | Out-Null
    Invoke-CaptureAdb -AdbArguments @('shell','am','force-stop','com.zhuorui.securities') | Out-Null
    Invoke-CaptureAdb -AdbArguments @('shell','am','start','-n','com.zhuorui.securities/.ui.SplashActivity') | Out-Null
    Write-Output 'Capture is ready on the configured test port. Sign in and complete verification locally when ready.'
    Write-Output 'This setup does not log in, operate order controls, or change the production listener.'
    Write-Output 'A manual new-device login may invalidate the other account session.'
    Write-Output 'When finished, run api_research\stop_capture.ps1.'
} catch {
    if ($StartedCapture) {
        try { & (Join-Path $ResearchRoot 'stop_capture.ps1') -ConfigPath $Settings.config_path } catch { Write-Warning 'Automatic test-session cleanup failed; inspect before continuing.' }
    }
    throw
}
