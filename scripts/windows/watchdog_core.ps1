. (Join-Path $PSScriptRoot 'recovery_common.ps1')
. (Join-Path $PSScriptRoot 'listener_common.ps1')

function Get-RecoveryObservation {
    param([string]$ProjectRoot, [string]$Component, $Intent)
    $Tracked = if ($Component -eq 'api') {
        Get-TrackedListener -Paths (Get-ListenerPaths $ProjectRoot 'api') -Backend 'api'
    } else { Get-TrackedMonitor $ProjectRoot }
    if ($Tracked -and -not $Tracked.Verified) { return @{ state='unverified'; healthy=$false } }
    if (-not $Tracked) {
        # A missing PID file is not proof of exit. Check for an orphan before launch.
        $Script = Join-Path $ProjectRoot $(if ($Component -eq 'api') { 'zhuorui_api.py' } else { 'zhuorui_monitor.py' })
        $Candidates = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -OperationTimeoutSec 10 -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Script) })
        if ($Candidates.Count) { return @{ state='unverified'; healthy=$false } }
        return @{ state='stopped'; healthy=$false }
    }
    $Healthy = $true
    if ($Component -eq 'api') {
        # Broker/login errors and old publications are not process failures.
        try {
            $State = Get-Content -LiteralPath $Tracked.Run.state_file -Raw | ConvertFrom-Json
            $Age = ([datetime]::UtcNow - ([datetime]$State.updated_at).ToUniversalTime()).TotalSeconds
            $Healthy = $State.running -eq $true -and $Age -ge -60 -and $Age -lt 180 -and
                ([datetime]$State.started_at).ToUniversalTime() -ge $Tracked.Process.StartTime.ToUniversalTime().AddSeconds(-30)
        } catch { $Healthy = $false }
    } else {
        try {
            $Python = Get-RecoveryPython $ProjectRoot
            if (-not $Python) { throw 'Independent Python is not configured.' }
            $Port = [int]$Intent.parameters.Port
            # Loopback TLS probe; never trust an external URL as process health.
            $ProbeUrl = "https://127.0.0.1:$Port/healthz"
            $null = & $Python -c "import ssl,sys,urllib.request; r=urllib.request.urlopen(sys.argv[1],context=ssl._create_unverified_context(),timeout=3); sys.exit(0 if r.status==200 else 1)" $ProbeUrl 2>$null
            $Healthy = $LASTEXITCODE -eq 0
        } catch { $Healthy = $false }
    }
    return @{ state='running'; healthy=$Healthy; pid=$Tracked.Pid; started_utc=$Tracked.Process.StartTime.ToUniversalTime().ToString('o') }
}

function Invoke-RecoveryLaunch {
    param([string]$ProjectRoot, [string]$Component)
    $Name = if ($Component -eq 'api') { 'start_zhuorui_listener.ps1' } else { 'start_zhuorui_monitor.ps1' }
    & (Join-Path $ProjectRoot "scripts\windows\$Name") -Recovery
}

function Request-RecoveryStop {
    param([string]$ProjectRoot, [string]$Component, $Observation, $Intent)
    $Name = if ($Component -eq 'api') { 'stop_zhuorui_listener.ps1' } else { 'stop_zhuorui_monitor.ps1' }
    & (Join-Path $ProjectRoot "scripts\windows\$Name") -Recovery -IntentRevision $Intent.revision -ExpectedPid $Observation.pid -ExpectedStartedUtc $Observation.started_utc
}

function Invoke-RecoveryTick {
    param([string]$ProjectRoot, [datetime]$Now = [datetime]::UtcNow)
    $Directory = Get-RecoveryDirectory $ProjectRoot
    $Settings = Get-Content -LiteralPath (Join-Path $Directory 'settings.json') -Raw | ConvertFrom-Json
    if ($Settings.version -ne 1 -or $Settings.enabled -isnot [bool]) { throw 'Invalid watchdog settings.' }
    if (-not $Settings.enabled) { return }
    $Lock = Enter-ServiceControlLock $ProjectRoot 'watchdog' -TimeoutSeconds 0
    try {
        $StatusPath = Join-Path $Directory 'status.json'
        $Previous = if (Test-Path -LiteralPath $StatusPath) { Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json } else { $null }
        $Status = @{ version=1; checked_at=$Now.ToUniversalTime().ToString('o'); components=@{} }
        foreach ($Component in @('monitor','api')) {
            $Entry = @{ state='attention'; failures=0; unhealthy_checks=0; restart_count=0; next_attempt_at=$null; healthy_since=$null; message='Status could not be verified.' }
            $Old = if ($Previous) { $Previous.components.$Component } else { $null }
            try {
                $Intent = Read-RecoveryIntent $ProjectRoot $Component
                if (-not $Intent) { throw 'No saved intent.' }
                $Entry.revision = $Intent.revision
                $Entry.desired_running = $Intent.desired_running
                if ($Old -and $Old.revision -eq $Intent.revision) {
                    foreach ($Key in @('failures','unhealthy_checks','restart_count','next_attempt_at','healthy_since','started_utc','stop_requested')) {
                        if ($null -ne $Old.$Key) { $Entry[$Key] = $Old.$Key }
                    }
                }
                if (-not $Intent.desired_running) {
                    $Entry.state='paused'; $Entry.message='Intentionally stopped; automatic start is paused.'
                    # Finish a Stop whose caller died after persisting the intent.
                    $Observation = Get-RecoveryObservation $ProjectRoot $Component $Intent
                    if ($Observation.state -eq 'running') {
                        try { Request-RecoveryStop $ProjectRoot $Component $Observation $Intent }
                        catch { $Entry.state='attention'; $Entry.message='Stop remains requested; waiting for the process to exit.' }
                    } elseif ($Observation.state -eq 'unverified') {
                        $Entry.state='attention'; $Entry.message='Stop requested, but process identity is uncertain; no process was touched.'
                    }
                } else {
                    $Observation = Get-RecoveryObservation $ProjectRoot $Component $Intent
                    if ($Observation.state -eq 'unverified') {
                        $Entry.state='attention'; $Entry.message='Process identity is uncertain; no replacement was started.'
                    } elseif ($Observation.state -eq 'running') {
                        $Entry.pid = $Observation.pid
                        if ($Entry.started_utc -ne $Observation.started_utc) {
                            $Entry.healthy_since=$null; $Entry.unhealthy_checks=0; $Entry.stop_requested=$false
                        }
                        $Entry.started_utc=$Observation.started_utc
                        if ($Observation.healthy -and -not $Entry.stop_requested) {
                            $Entry.state='running'; $Entry.message='Running; crash recovery is active.'; $Entry.unhealthy_checks=0
                            if (-not $Entry.healthy_since) { $Entry.healthy_since=$Now.ToString('o') }
                            if (($Now - [datetime]$Entry.healthy_since).TotalSeconds -ge 600) { $Entry.failures=0; $Entry.next_attempt_at=$null }
                        } else {
                            $Entry.healthy_since=$null; $Entry.unhealthy_checks++
                            $Entry.state='checking'; $Entry.message='Waiting for process health to recover.'
                            if ($Entry.unhealthy_checks -ge 3) {
                                $Entry.state='attention'; $Entry.message='Graceful stop requested; waiting for confirmed exit. No force kill.'
                                if (-not $Entry.stop_requested) {
                                    $Entry.stop_requested=$true
                                    try { Request-RecoveryStop $ProjectRoot $Component $Observation $Intent } catch { }
                                }
                            }
                        }
                    } else {
                        $Entry.healthy_since=$null; $Entry.stop_requested=$false; $Entry.unhealthy_checks=0
                        if ($Entry.next_attempt_at -and $Now -lt [datetime]$Entry.next_attempt_at) {
                            $Entry.state='retry_wait'; $Entry.message='Stopped; waiting before the next restart attempt.'
                        } else {
                            $Entry.failures=[math]::Min(1000, (1 + [int]$Entry.failures))
                            $Delay = @(60,120,240,300)[[math]::Min(3, $Entry.failures - 1)]
                            $Entry.next_attempt_at=$Now.AddSeconds($Delay).ToString('o')
                            $Entry.restart_count++
                            $Entry.state='starting'; $Entry.message='Restart requested; awaiting a healthy process.'
                            # Reserve backoff durably before dispatch, including watchdog crashes.
                            $Status.components[$Component]=$Entry
                            if ($Previous) {
                                foreach ($Other in @('monitor','api')) {
                                    if (-not $Status.components.ContainsKey($Other) -and $Previous.components.$Other) { $Status.components[$Other]=$Previous.components.$Other }
                                }
                            }
                            Write-RecoveryJson $StatusPath $Status
                            Write-RecoveryEvent $ProjectRoot $Component 'restart_attempt' 'The process is stopped; requesting a restart.'
                            try { Invoke-RecoveryLaunch $ProjectRoot $Component }
                            catch { $Entry.state='retry_wait'; $Entry.message='Launch failed; inspect the component startup log. Retry is scheduled.' }
                        }
                    }
                }
            } catch {
                $Entry.state='attention'; $Entry.message='Recovery configuration or process status could not be verified; no automatic launch.'
            }
            $Status.components[$Component]=$Entry
            if (-not $Old -or $Old.state -ne $Entry.state) { Write-RecoveryEvent $ProjectRoot $Component $Entry.state $Entry.message }
        }
        Write-RecoveryJson $StatusPath $Status
    } finally { $Lock.Dispose() }
}
