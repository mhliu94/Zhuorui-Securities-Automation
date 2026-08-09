param(
    [int]$Port = 443,
    [int]$RedirectHttpPort = 80,
    [string]$HostAddress = "0.0.0.0",
    [string]$PublicHost,
    [ValidateRange(10, 86400)]
    [int]$Interval = 60,
    [string]$CertificatePath,
    [string]$PrivateKeyPath,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerPath = Join-Path $Root "zhuorui_monitor.py"
$PidPath = Join-Path $Root "zhuorui_monitor.pid"
$CurrentRunPath = Join-Path $Root "zhuorui_monitor.current.json"
$LogDir = Join-Path $Root "logs"
$DefaultCertificatePath = Join-Path $Root "certs\zhuorui-monitor-cert.pem"
$DefaultPrivateKeyPath = Join-Path $Root "certs\zhuorui-monitor-key.pem"
$ConfigPath = Join-Path $Root "zhuorui_config.json"
if (-not $CertificatePath) { $CertificatePath = $DefaultCertificatePath }
if (-not $PrivateKeyPath) { $PrivateKeyPath = $DefaultPrivateKeyPath }
if (-not $PublicHost) {
    $ProbeSocket = $null
    try {
        $ProbeAddress = [System.Net.Dns]::GetHostAddresses("example.com") |
            Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
            Select-Object -First 1
        if ($ProbeAddress) {
            $ProbeSocket = New-Object System.Net.Sockets.Socket(
                [System.Net.Sockets.AddressFamily]::InterNetwork,
                [System.Net.Sockets.SocketType]::Dgram,
                [System.Net.Sockets.ProtocolType]::Udp
            )
            $ProbeSocket.Connect($ProbeAddress, 443)
            $PublicHost = $ProbeSocket.LocalEndPoint.Address.IPAddressToString
        }
    } catch {
        $PublicHost = $null
    } finally {
        if ($ProbeSocket) { $ProbeSocket.Dispose() }
    }
}
if (-not $PublicHost -and (Test-Path -LiteralPath $ConfigPath)) {
    try {
        $MonitorConfig = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        $PublicHost = [string]$MonitorConfig.public_host
    } catch {
        $PublicHost = $null
    }
}
if (-not $PublicHost) { $PublicHost = "localhost" }
$UrlHost = if ($HostAddress -eq "0.0.0.0" -or $HostAddress -eq "::") { "localhost" } else { $HostAddress }
$Url = if ($Port -eq 443) { "https://${UrlHost}/" } else { "https://${UrlHost}:$Port/" }

if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535."
}
if ($RedirectHttpPort -lt 1 -or $RedirectHttpPort -gt 65535 -or $RedirectHttpPort -eq $Port) {
    throw "RedirectHttpPort must be between 1 and 65535 and different from Port."
}

if (-not (Test-Path -LiteralPath $CertificatePath) -or -not (Test-Path -LiteralPath $PrivateKeyPath)) {
    & (Join-Path $Root "setup_zhuorui_monitor_https.ps1")
}

if (Test-Path -LiteralPath $PidPath) {
    $ExistingPid = (Get-Content -LiteralPath $PidPath -Raw).Trim()
    if ($ExistingPid -match '^\d+$') {
        $ExistingProcess = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            $ExistingIsMonitor = $false
            if (Test-Path -LiteralPath $CurrentRunPath) {
                try {
                    $CurrentRun = Get-Content -LiteralPath $CurrentRunPath -Raw | ConvertFrom-Json
                    $RecordedStart = ([datetime]$CurrentRun.started_utc).ToUniversalTime()
                    $ActualStart = $ExistingProcess.StartTime.ToUniversalTime()
                    $ExistingIsMonitor = `
                        ([int]$CurrentRun.pid -eq [int]$ExistingPid) -and `
                        ([math]::Abs(($RecordedStart - $ActualStart).TotalSeconds) -le 30)
                } catch {
                    $ExistingIsMonitor = $false
                }
            }
            if ($ExistingIsMonitor) {
                Write-Host "Zhuorui Control Room is already running with PID $ExistingPid."
                Write-Host $Url
                if ($OpenBrowser) {
                    Start-Process $Url
                }
                exit 0
            }
            Write-Host "Monitor PID file was stale; process $ExistingPid was left untouched."
        }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
}

$ProjectPy = Join-Path $Root ".venv\Scripts\python.exe"
$BundledPy = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$VenvConfig = Join-Path $Root ".venv\pyvenv.cfg"
$BasePy = $null
if (Test-Path -LiteralPath $VenvConfig) {
    $HomeLine = Get-Content -LiteralPath $VenvConfig | Where-Object { $_ -match '^home\s*=\s*(.+)$' } | Select-Object -First 1
    if ($HomeLine -match '^home\s*=\s*(.+)$') {
        $BasePy = Join-Path $Matches[1].Trim() "python.exe"
    }
}
if ($BasePy -and (Test-Path -LiteralPath $BasePy)) {
    $PythonExe = $BasePy
} elseif (Test-Path -LiteralPath $BundledPy) {
    $PythonExe = $BundledPy
} elseif (Test-Path -LiteralPath $ProjectPy) {
    $PythonExe = $ProjectPy
} else {
    $PythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCmd) {
        $PythonExe = $PythonCmd.Source
    } else {
        $PyCmd = Get-Command py -ErrorAction SilentlyContinue
        if (-not $PyCmd) {
            throw "Could not find Python. Install Python or use the project virtual environment."
        }
        $PythonExe = $PyCmd.Source
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$RunStartedUtc = (Get-Date).ToUniversalTime()
$RunStamp = $RunStartedUtc.ToString("yyyyMMdd'T'HHmmss'Z'")
$OutLog = Join-Path $LogDir "zhuorui_monitor_$RunStamp.out.log"
$ErrLog = Join-Path $LogDir "zhuorui_monitor_$RunStamp.err.log"

# Some Windows hosts expose both Path and PATH. Start-Process treats them as a
# duplicate dictionary key, so normalize the process environment first.
$ProcessPathValue = $env:Path
[System.Environment]::SetEnvironmentVariable("PATH", $null, [System.EnvironmentVariableTarget]::Process)
[System.Environment]::SetEnvironmentVariable("Path", $ProcessPathValue, [System.EnvironmentVariableTarget]::Process)

$Process = Start-Process `
    -WindowStyle Hidden `
    -WorkingDirectory $Root `
    -FilePath $PythonExe `
    -ArgumentList @("`"$ServerPath`"", "--host", "`"$HostAddress`"", "--port", "$Port", "--redirect-http-port", "$RedirectHttpPort", "--public-host", "`"$PublicHost`"", "--interval", "$Interval", "--cert-file", "`"$CertificatePath`"", "--key-file", "`"$PrivateKeyPath`"") `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

$Process.Id | Set-Content -LiteralPath $PidPath -Encoding ascii
[ordered]@{
    pid = $Process.Id
    started_utc = $RunStartedUtc.ToString("o")
    url = $Url
    external_url = if ($Port -eq 443) { "https://${PublicHost}/" } else { "https://${PublicHost}:$Port/" }
    stdout = $OutLog
    stderr = $ErrLog
} | ConvertTo-Json | Set-Content -LiteralPath $CurrentRunPath -Encoding utf8

$Ready = $false
for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
    Start-Sleep -Milliseconds 250
    if ($Process.HasExited) {
        break
    }
    try {
        & $PythonExe -c "import ssl,sys,urllib.request; urllib.request.urlopen(sys.argv[1], context=ssl._create_unverified_context(), timeout=1).read()" ($Url + "healthz") | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $Ready = $true
            break
        }
    } catch {
        # The dashboard is still starting.
    }
}

if (-not $Ready) {
    if ($Process.HasExited) {
        Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
        $Details = if (Test-Path -LiteralPath $ErrLog) { (Get-Content -LiteralPath $ErrLog -Raw).Trim() } else { "" }
        throw "Zhuorui Control Room exited during startup. $Details"
    }
    Write-Warning "The process started, but the dashboard did not answer within five seconds. Check $ErrLog"
}

Write-Host "Started Zhuorui Control Room with PID $($Process.Id)."
Write-Host $Url
if ($OpenBrowser) {
    Start-Process $Url
}
