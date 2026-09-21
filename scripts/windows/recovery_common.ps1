# Shared by manual controls and the Windows watchdog. No actions on import.
function Get-RecoveryDirectory {
    param([string]$ProjectRoot)
    return (Join-Path $ProjectRoot 'runtime\recovery')
}

function Enter-ServiceControlLock {
    param([string]$ProjectRoot, [string]$Component, [int]$TimeoutSeconds = 35)
    $Directory = Get-RecoveryDirectory $ProjectRoot
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $Deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            return [IO.File]::Open((Join-Path $Directory "$Component.lock"),
                [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        } catch [IO.IOException] {
            if ([datetime]::UtcNow -ge $Deadline) { throw 'Another service control operation is still running.' }
            Start-Sleep -Milliseconds 100
        }
    } while ($true)
}

function Write-RecoveryJson {
    param([string]$Path, $Value)
    $Directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $Temporary = Join-Path $Directory ([IO.Path]::GetRandomFileName())
    try {
        [IO.File]::WriteAllText($Temporary, ($Value | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($Temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($Temporary, $Path) }
    } finally {
        if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force }
    }
}

function Read-RecoveryIntent {
    param([string]$ProjectRoot, [ValidateSet('api','monitor')][string]$Component)
    $Path = Join-Path (Get-RecoveryDirectory $ProjectRoot) "$Component.intent.json"
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $Intent = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($Intent.version -ne 1 -or $Intent.desired_running -isnot [bool] -or
        -not $Intent.revision -or $null -eq $Intent.parameters) { throw 'Service intent is invalid; automatic launch was refused.' }
    return $Intent
}

function Set-RecoveryIntent {
    # Caller holds the component control lock. Stop persists even if no process exists.
    param([string]$ProjectRoot, [ValidateSet('api','monitor')][string]$Component,
          [bool]$Running, [hashtable]$Parameters)
    if ($null -eq $Parameters) {
        try { $Previous = Read-RecoveryIntent $ProjectRoot $Component }
        catch {
            if ($Running) { throw }
            # An explicit Stop must remain possible even with damaged recovery metadata.
            $Previous = $null
        }
        $Parameters = @{}
        if ($Previous) { foreach ($Property in $Previous.parameters.PSObject.Properties) { $Parameters[$Property.Name] = $Property.Value } }
    }
    Write-RecoveryJson (Join-Path (Get-RecoveryDirectory $ProjectRoot) "$Component.intent.json") @{
        version = 1; desired_running = $Running; revision = [guid]::NewGuid().ToString()
        updated_at = [datetime]::UtcNow.ToString('o'); parameters = $Parameters
    }
}

function Get-RecoveryPython {
    param([string]$ProjectRoot)
    $Python = Join-Path $ProjectRoot 'runtime\python-env\Scripts\python.exe'
    if (Test-Path -LiteralPath $Python -PathType Leaf) { return $Python }
    return $null
}

function Get-TrackedMonitor {
    param([string]$ProjectRoot)
    $PidPath = Join-Path $ProjectRoot 'zhuorui_monitor.pid'
    if (-not (Test-Path -LiteralPath $PidPath)) { return $null }
    $SavedPid = (Get-Content -LiteralPath $PidPath -Raw).Trim()
    if ($SavedPid -notmatch '^\d+$') { throw 'Monitor PID file is invalid.' }
    $Process = Get-Process -Id ([int]$SavedPid) -ErrorAction SilentlyContinue
    if (-not $Process) { return $null }
    $Verified = $false
    $Run = $null
    try {
        $Run = Get-Content -LiteralPath (Join-Path $ProjectRoot 'zhuorui_monitor.current.json') -Raw | ConvertFrom-Json
        $Verified = ([int]$Run.pid -eq [int]$SavedPid) -and
            ([math]::Abs((([datetime]$Run.started_utc).ToUniversalTime() - $Process.StartTime.ToUniversalTime()).TotalSeconds) -le 30)
        if ($Run.script) { $Verified = $Verified -and ($Run.script -eq (Join-Path $ProjectRoot 'zhuorui_monitor.py')) }
        if ($Run.python -and $Process.Path) { $Verified = $Verified -and ($Run.python -eq $Process.Path) }
    } catch { $Verified = $false }
    return @{ Process = $Process; Pid = [int]$SavedPid; Run = $Run; Verified = $Verified }
}

function Write-RecoveryEvent {
    param([string]$ProjectRoot, [string]$Component, [string]$Event, [string]$Message)
    $Directory = Join-Path $ProjectRoot 'logs'
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    # Fixed categories/messages only: no credentials, raw broker responses, or exception text.
    $Line = @{ time_utc = [datetime]::UtcNow.ToString('o'); component = $Component; event = $Event; message = $Message } | ConvertTo-Json -Compress
    Add-Content -LiteralPath (Join-Path $Directory ('watchdog_' + [datetime]::UtcNow.ToString('yyyyMMdd') + '.log')) -Value $Line -Encoding utf8
}
