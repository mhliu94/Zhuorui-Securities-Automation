param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'capture_common.ps1')
$Settings = Get-CaptureSettings -ConfigPath $ConfigPath
$ResearchRoot = $PSScriptRoot
if (Get-NetTCPConnection -LocalPort $Settings.proxy_port -State Listen -ErrorAction SilentlyContinue) {
    throw 'The configured proxy port is already in use. Inspect it before starting another proxy.'
}
New-Item -ItemType Directory -Force -Path $Settings.private_dir | Out-Null
$SavedPrivate = $env:ZHUORUI_CAPTURE_PRIVATE
try {
$env:ZHUORUI_CAPTURE_PRIVATE = $Settings.private_dir
$Process = Start-Process -FilePath ($Settings.mitmdump_executable) `
    -ArgumentList (ConvertTo-ProcessArguments @('--listen-host',$Settings.proxy_listen_host,'--listen-port',[string]$Settings.proxy_port,
        '--allow-hosts','backendpro\.zr\.hk:443','--set','connection_strategy=lazy',
        '--set',('confdir=' + (Join-Path $Settings.private_dir 'mitmproxy')),
        '-s',(Join-Path $ResearchRoot 'capture_api.py'))) `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $Settings.private_dir 'api-proxy.out.log') `
    -RedirectStandardError (Join-Path $Settings.private_dir 'api-proxy.err.log')
$Process.Id | Set-Content -LiteralPath (Join-Path $Settings.private_dir 'api-proxy.pid')
Write-Output "Local HTTPS capture proxy started; PID $($Process.Id)."
} finally { $env:ZHUORUI_CAPTURE_PRIVATE = $SavedPrivate }
