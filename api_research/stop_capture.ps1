param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath
$ResearchRoot = $PSScriptRoot
# Restore original connectivity before stopping the proxy, when it is in use.
$OriginalSessionPath = Join-Path $Settings.private_dir 'original-capture-session.json'
if (Test-Path -LiteralPath $OriginalSessionPath) {
    $OriginalSession = Get-Content -Raw -LiteralPath $OriginalSessionPath | ConvertFrom-Json
    if ($OriginalSession.phase -notin @('restored','backed_up','backing_up')) {
        & ($Settings.python_executable) (Join-Path $ResearchRoot 'restore_original_proxy.py') --config $Settings.config_path
        if ($LASTEXITCODE -ne 0) { throw 'Original proxy restoration failed; capture proxy remains running.' }
    }
}
$Devices = & $Settings.adb devices
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect emulator state; proxy remains running.' }
if ($Devices -match ([regex]::Escape($Settings.test_device) + '\s+device')) {
    $AvdName = (& $Settings.adb -s $Settings.test_device emu avd name | Select-Object -First 1).Trim()
    if ($AvdName -ne $Settings.test_avd) { throw 'Unexpected emulator on the configured test port; refusing shutdown.' }
    & $Settings.adb -s $Settings.test_device shell settings put global http_proxy :0
    if ($LASTEXITCODE -ne 0) { throw 'Could not clear the test emulator proxy.' }
    & $Settings.adb -s $Settings.test_device emu kill
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the test emulator.' }
}
$PidFile = Join-Path $Settings.private_dir 'api-proxy.pid'
if (Test-Path -LiteralPath $PidFile) {
    $ProxyPid = [int](Get-Content -Raw -LiteralPath $PidFile)
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProxyPid"
    if ($Process) {
        $ExpectedExecutable = $Settings.mitmdump_executable
        if ($Process.ExecutablePath -ne $ExpectedExecutable -or $Process.CommandLine -notmatch ('--listen-port"?\s+"?' + $Settings.proxy_port + '"?(?:\s|$)')) {
            throw 'Proxy PID belongs to an unexpected process; refusing shutdown.'
        }
        & taskkill.exe /PID $ProxyPid /T /F
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop the capture proxy process tree.' }
    }
    Remove-Item -LiteralPath $PidFile
}
Write-Output 'Capture proxy and separate test capture services are stopped. Any original capture proxy settings were restored.'
if ($OriginalSession -and $OriginalSession.phase -notin @('restored','backed_up','backing_up')) {
    Write-Output 'The original AVD still needs a normal cold start to remove temporary debugging and certificate mounts.'
}
