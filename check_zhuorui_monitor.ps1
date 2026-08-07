param(
    [int]$Port = 443,
    [string]$HostAddress = "localhost"
)

$ErrorActionPreference = "Stop"
$Url = if ($Port -eq 443) { "https://${HostAddress}/healthz" } else { "https://${HostAddress}:$Port/healthz" }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectPy = Join-Path $Root ".venv\Scripts\python.exe"
$BundledPy = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path -LiteralPath $ProjectPy) {
    $PythonExe = $ProjectPy
} elseif (Test-Path -LiteralPath $BundledPy) {
    $PythonExe = $BundledPy
} else {
    $PythonCommand = Get-Command python -ErrorAction Stop
    $PythonExe = $PythonCommand.Source
}

try {
    $Status = & $PythonExe -c "import ssl,sys,urllib.request; print(urllib.request.urlopen(sys.argv[1], context=ssl._create_unverified_context(), timeout=5).read().decode())" $Url
    if ($LASTEXITCODE -ne 0) {
        throw "HTTPS health check failed."
    }
} catch {
    Write-Host "Zhuorui Control Room is not responding at $Url"
    exit 1
}

Write-Host "Authenticated Zhuorui Control Room is responding over HTTPS."
