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
            $ExistingIsListener = $false
            $CurrentRun = $null
            if (Test-Path $CurrentRunPath) {
                try {
                    $CurrentRun = Get-Content $CurrentRunPath -Raw | ConvertFrom-Json
                    $RecordedStart = ([datetime]$CurrentRun.started_utc).ToUniversalTime()
                    $ActualStart = $ExistingProcess.StartTime.ToUniversalTime()
                    $ExistingIsListener = `
                        ([int]$CurrentRun.pid -eq [int]$ExistingPid) -and `
                        ([math]::Abs(($RecordedStart - $ActualStart).TotalSeconds) -le 30)
                } catch {
                    $ExistingIsListener = $false
                }
            }
            if ($ExistingIsListener) {
                Write-Host "Zhuorui listener is already running with PID $ExistingPid."
                Write-Host "stdout: $($CurrentRun.stdout)"
                Write-Host "stderr: $($CurrentRun.stderr)"
                exit 0
            }
            Write-Host "Listener PID file was stale; process $ExistingPid was left untouched."
        }
    }
    Remove-Item -LiteralPath $PidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $CurrentRunPath -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$ProjectPy = Join-Path $Root ".venv\Scripts\python.exe"
$BundledPy = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path $ProjectPy) {
    $PythonExe = $ProjectPy
} elseif (Test-Path $BundledPy) {
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

# Some Windows hosts expose both Path and PATH. Start-Process treats them as a
# duplicate dictionary key, so normalize the process environment first.
$ProcessPathValue = $env:Path
[System.Environment]::SetEnvironmentVariable("PATH", $null, [System.EnvironmentVariableTarget]::Process)
[System.Environment]::SetEnvironmentVariable("Path", $ProcessPathValue, [System.EnvironmentVariableTarget]::Process)

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
