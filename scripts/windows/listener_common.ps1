# Shared process identity and configuration helpers. Dot sourcing has no side effects.
function Get-ListenerPaths {
    param([string]$ProjectRoot, [ValidateSet('api', 'ui')][string]$Backend = 'api')
    $Name = if ($Backend -eq 'api') { 'zhuorui_api_listener' } else { 'zhuorui_listener' }
    return @{
        Name = $Name
        Pid = Join-Path $ProjectRoot "$Name.pid"
        Current = Join-Path $ProjectRoot "$Name.current.json"
        Script = Join-Path $ProjectRoot $(if ($Backend -eq 'api') { 'zhuorui_api.py' } else { 'zhuorui_market_order.py' })
    }
}

function Get-TrackedListener {
    param([hashtable]$Paths, [ValidateSet('api', 'ui')][string]$Backend)
    if (-not (Test-Path -LiteralPath $Paths.Pid)) { return $null }
    $SavedPid = (Get-Content -LiteralPath $Paths.Pid -Raw).Trim()
    if ($SavedPid -notmatch '^\d+$') { return $null }
    $SavedProcess = Get-Process -Id ([int]$SavedPid) -ErrorAction SilentlyContinue
    if (-not $SavedProcess) { return $null }
    $Verified = $false
    $SavedRun = $null
    try {
        $SavedRun = Get-Content -LiteralPath $Paths.Current -Raw | ConvertFrom-Json
        $RecordedStart = ([datetime]$SavedRun.started_utc).ToUniversalTime()
        $ActualStart = $SavedProcess.StartTime.ToUniversalTime()
        $Verified = ([int]$SavedRun.pid -eq [int]$SavedPid) -and `
            ([math]::Abs(($RecordedStart - $ActualStart).TotalSeconds) -le 30)
        if ($Backend -eq 'api') {
            $Verified = $Verified -and ($SavedRun.backend -eq 'api') -and `
                ([string]$SavedRun.script -eq $Paths.Script)
        } elseif ($SavedRun.backend -and $SavedRun.backend -ne 'ui') {
            $Verified = $false
        }
    } catch { $Verified = $false }
    return @{ Process = $SavedProcess; Run = $SavedRun; Verified = $Verified; Pid = [int]$SavedPid }
}

function Resolve-ListenerConfigPath {
    param([string]$Value, [string]$BaseDirectory)
    $Expanded = $Value
    if ($Expanded -eq '~') {
        $Expanded = $env:USERPROFILE
    } elseif ($Expanded.StartsWith('~\') -or $Expanded.StartsWith('~/')) {
        $Expanded = Join-Path $env:USERPROFILE $Expanded.Substring(2)
    }
    if (-not [System.IO.Path]::IsPathRooted($Expanded)) {
        $Expanded = Join-Path $BaseDirectory $Expanded
    }
    return [System.IO.Path]::GetFullPath($Expanded)
}

function Get-ApiListenerPython {
    param([string]$ProjectRoot, [string]$ConfigDirectory, $ApiConfig)
    $Executable = if ($ApiConfig.python_executable) {
        Resolve-ListenerConfigPath -Value ([string]$ApiConfig.python_executable) -BaseDirectory $ConfigDirectory
    } else {
        Join-Path $ProjectRoot '.venv-api\Scripts\python.exe'
    }
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw 'API Python was not found. Install requirements-api.txt in .venv-api, or set api.python_executable.'
    }
    return $Executable
}

function ConvertTo-ListenerBoolean {
    param($Value, [bool]$Default)
    if ($null -eq $Value -or ($Value -is [string] -and $Value -eq '')) { return $Default }
    if ($Value -is [bool]) { return $Value }
    if ($Value -is [string]) {
        switch ($Value.Trim().ToLowerInvariant()) {
            { $_ -in '1', 'true', 'yes', 'on' } { return $true }
            { $_ -in '0', 'false', 'no', 'off' } { return $false }
        }
    }
    throw 'Listener trading switches must be booleans.'
}

function Get-ApiListenerOrderMode {
    param($Config)
    $TradingEnabled = ConvertTo-ListenerBoolean -Value $Config.trading_enabled -Default $true
    $TradingEnabled = ConvertTo-ListenerBoolean -Value $Config.account.trading_enabled -Default $TradingEnabled
    $ApiEnabled = ConvertTo-ListenerBoolean -Value $Config.api.live_orders_enabled -Default $true
    return $ApiEnabled -and $TradingEnabled
}
