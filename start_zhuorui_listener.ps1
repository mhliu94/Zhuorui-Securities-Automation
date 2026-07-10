param(
    [string]$ConfigPath = ".\zhuorui_config.json"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = Join-Path $Root "zhuorui_market_order.py"
$PidPath = Join-Path $Root "zhuorui_listener.pid"
$CurrentRunPath = Join-Path $Root "zhuorui_listener.current.json"
$LogDir = Join-Path $Root "logs"
$RunStartedUtc = (Get-Date).ToUniversalTime()
$RunStamp = $RunStartedUtc.ToString("yyyyMMdd'T'HHmmss'Z'")
$OutLog = Join-Path $LogDir "zhuorui_listener_$RunStamp.out.log"
$ErrLog = Join-Path $LogDir "zhuorui_listener_$RunStamp.err.log"
$ConfigInputPath = if ([System.IO.Path]::IsPathRooted($ConfigPath)) {
    $ConfigPath
} else {
    Join-Path $Root $ConfigPath
}
$ResolvedConfig = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
    $ConfigInputPath
)

if (Test-Path $PidPath) {
    $ExistingPid = (Get-Content $PidPath -Raw).Trim()
    if ($ExistingPid -match '^\d+$') {
        $ExistingProcess = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
        if ($ExistingProcess) {
            Write-Host "Zhuorui listener is already running with PID $ExistingPid."
            if (Test-Path $CurrentRunPath) {
                $CurrentRun = Get-Content $CurrentRunPath -Raw | ConvertFrom-Json
                Write-Host "stdout: $($CurrentRun.stdout)"
                Write-Host "stderr: $($CurrentRun.stderr)"
            }
            exit 0
        }
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$BundledPy = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $BundledPy) {
    $PythonExe = $BundledPy
} else {
    $PythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($PythonCmd) {
        $PythonExe = $PythonCmd.Source
    } else {
        $PyCmd = Get-Command py -ErrorAction SilentlyContinue
        if (-not $PyCmd) {
            throw "Could not find Python. Install Python or use the bundled Codex runtime."
        }
        $PythonExe = $PyCmd.Source
    }
}

$Process = Start-Process `
    -WindowStyle Hidden `
    -WorkingDirectory $Root `
    -FilePath $PythonExe `
    -ArgumentList @("`"$ScriptPath`"", "server", "--config", "`"$ResolvedConfig`"") `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

$Process.Id | Set-Content -Path $PidPath -Encoding ascii
$RunInfo = [ordered]@{
    pid = $Process.Id
    started_utc = $RunStartedUtc.ToString("o")
    stdout = $OutLog
    stderr = $ErrLog
    config = $ResolvedConfig
}
$RunInfo | ConvertTo-Json | Set-Content -Path $CurrentRunPath -Encoding utf8
Write-Host "Started Zhuorui listener with PID $($Process.Id)."
Write-Host "stdout: $OutLog"
Write-Host "stderr: $ErrLog"
