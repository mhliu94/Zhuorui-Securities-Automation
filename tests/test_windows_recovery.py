"""Recovery tests use temporary projects, fake processes, and no broker writes."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from zhuorui.monitor.recovery import recovery_status


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "windows"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class WindowsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts" / "windows"
        shutil.copytree(SOURCE, self.scripts)
        self.recovery = self.root / "runtime" / "recovery"
        self.recovery.mkdir(parents=True)
        self.write("settings.json", {"version": 1, "enabled": True, "logon_type": "Password"})
        for component in ("api", "monitor"):
            self.intent(component, component == "api")

    def write(self, name, data):
        (self.recovery / name).write_text(json.dumps(data))

    def intent(self, component, running, revision="original"):
        self.write(f"{component}.intent.json", {
            "version": 1, "desired_running": running, "revision": revision,
            "parameters": {"ConfigPath": str(self.root / "zhuorui_config.json")} if component == "api" else {"Port": 443},
        })

    def run_ps(self, code):
        test = self.root / "test.ps1"
        test.write_text("$ErrorActionPreference='Stop'\n"
                        ". (Join-Path $PSScriptRoot 'scripts\\windows\\watchdog_core.ps1')\n"
                        "$Project=$PSScriptRoot\n" + code)
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(test)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def status(self, component="api"):
        return json.loads((self.recovery / "status.json").read_text(encoding="utf-8-sig"))["components"][component]

    def test_crash_restart_backoff_survives_watchdog_restart(self):
        stubs = r'''
function Get-RecoveryObservation { return @{state='stopped'; healthy=$false} }
function Invoke-RecoveryLaunch { Add-Content (Join-Path $Project 'launches') 'launch'; throw 'synthetic startup failure' }
'''
        self.run_ps(stubs + "Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:00:00Z')")
        self.assertEqual(self.status()["restart_count"], 1)
        self.run_ps(stubs + "Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:00:30Z')")
        self.assertEqual(self.status()["restart_count"], 1)
        self.run_ps(stubs + "Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:01:00Z')")
        self.assertEqual(self.status()["failures"], 2)
        self.assertIn("00:03:00", self.status()["next_attempt_at"])
        self.assertEqual((self.root / "launches").read_text().splitlines(), ["launch", "launch"])

    def test_backoff_caps_at_five_minutes(self):
        self.run_ps(r'''
function Get-RecoveryObservation { return @{state='stopped'; healthy=$false} }
function Invoke-RecoveryLaunch { throw 'synthetic failure' }
foreach ($Minute in @(0,1,3,7,12,17)) { Invoke-RecoveryTick $Project (([datetime]'2026-09-21T00:00:00Z').AddMinutes($Minute)) }
''')
        self.assertEqual(self.status()["restart_count"], 6)
        self.assertIn("00:22:00", self.status()["next_attempt_at"])

    def test_intentional_stop_persists_across_watchdog_runs_and_resume(self):
        self.run_ps(r'''
function Get-Process { return $null }
& (Join-Path $Project 'scripts\windows\stop_zhuorui_listener.ps1')
''')
        self.assertFalse(json.loads((self.recovery / "api.intent.json").read_text())["desired_running"])
        self.run_ps(r'''
function Get-RecoveryObservation { return @{state='stopped'; healthy=$false} }
function Invoke-RecoveryLaunch { throw 'Must not launch paused service' }
Invoke-RecoveryTick $Project
''')
        self.assertEqual(self.status()["state"], "paused")
        self.intent("api", True, "resumed")
        self.run_ps(r'''
function Get-RecoveryObservation { return @{state='stopped'; healthy=$false} }
function Invoke-RecoveryLaunch { Set-Content (Join-Path $Project 'resumed') 'yes' }
Invoke-RecoveryTick $Project
''')
        self.assertTrue((self.root / "resumed").exists())

    def test_automatic_launcher_rechecks_stop_inside_control_lock(self):
        self.intent("api", False)
        self.run_ps(r'''
function Start-Process { throw 'Stop was ignored' }
& (Join-Path $Project 'scripts\windows\start_zhuorui_listener.ps1') -Recovery
''')
        self.assertFalse((self.root / "zhuorui_api_listener.pid").exists())

    def test_old_recovery_stop_cannot_override_new_manual_start(self):
        self.intent("api", True, "new-start")
        self.run_ps(r'''
function Get-Process { throw 'A stale stop must not inspect or touch a process' }
& (Join-Path $Project 'scripts\windows\stop_zhuorui_listener.ps1') -Recovery -IntentRevision 'old-stop' -ExpectedPid 999991 -ExpectedStartedUtc '2026-09-21T00:00:00Z'
''')
        self.assertTrue(json.loads((self.recovery / "api.intent.json").read_text())["desired_running"])
        self.assertFalse((self.root / "runtime" / "api" / "listener.stop").exists())

    def test_unverified_process_is_never_replaced(self):
        self.run_ps(r'''
function Get-RecoveryObservation { return @{state='unverified'; healthy=$false} }
function Invoke-RecoveryLaunch { throw 'Must not launch' }
Invoke-RecoveryTick $Project
''')
        self.assertEqual(self.status()["state"], "attention")
        self.assertEqual(self.status()["restart_count"], 0)

    def test_orphan_process_without_pid_file_blocks_automatic_launch(self):
        self.run_ps(r'''
function Get-CimInstance { return [pscustomobject]@{CommandLine=('python.exe "' + (Join-Path $Project 'zhuorui_api.py') + '" listen')} }
Invoke-RecoveryTick $Project
''')
        self.assertEqual(self.status()["state"], "attention")
        self.assertEqual(self.status()["restart_count"], 0)

    def test_logout_network_errors_and_stale_publications_do_not_restart_alive_api(self):
        self.run_ps(r'''
$StatePath=Join-Path $Project 'runtime\listener.json'
@{running=$true; updated_at=[datetime]::UtcNow.ToString('o'); started_at=[datetime]::UtcNow.AddMinutes(-20).ToString('o'); session_status='login_blocked'; last_error='Broker offline'; last_holdings_publish='2020-01-01'} | ConvertTo-Json | Set-Content $StatePath
function Get-TrackedListener { return @{Verified=$true; Pid=999991; Run=@{state_file=$StatePath}; Process=[pscustomobject]@{StartTime=[datetime]::UtcNow.AddMinutes(-20)}} }
function Invoke-RecoveryLaunch { throw 'Must not launch' }
Invoke-RecoveryTick $Project
''')
        self.assertEqual(self.status()["state"], "running")
        self.assertEqual(self.status()["restart_count"], 0)

    def test_hang_requests_graceful_stop_then_waits_for_exit(self):
        self.run_ps(r'''
function Get-RecoveryObservation { param($ProjectRoot,$Component); if($Component -eq 'monitor'){return @{state='stopped'}}; return @{state='running'; healthy=$false; pid=999991; started_utc='2026-09-20T00:00:00Z'} }
function Request-RecoveryStop { Add-Content (Join-Path $Project 'stops') 'stop'; throw 'Still finishing work' }
function Invoke-RecoveryLaunch { throw 'Must not launch while alive' }
foreach($Minute in 0..3){ Invoke-RecoveryTick $Project (([datetime]'2026-09-21T00:00:00Z').AddMinutes($Minute)) }
''')
        self.assertEqual(self.status()["state"], "attention")
        self.assertEqual((self.root / "stops").read_text().splitlines(), ["stop"])
        self.assertEqual(self.status()["restart_count"], 0)
        self.run_ps(r'''
function Get-RecoveryObservation { return @{state='stopped'; healthy=$false} }
function Invoke-RecoveryLaunch { Set-Content (Join-Path $Project 'after-exit') 'started' }
Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:04:00Z')
''')
        self.assertTrue((self.root / "after-exit").exists())
        self.assertTrue(json.loads((self.recovery / "api.intent.json").read_text())["desired_running"])

    def test_corrupt_intent_fails_closed(self):
        (self.recovery / "api.intent.json").write_text('{"desired_running":"yes"}')
        self.run_ps("Invoke-RecoveryTick $Project")
        self.assertEqual(self.status()["state"], "attention")
        self.assertEqual(self.status()["restart_count"], 0)

    def test_component_lock_rejects_overlapping_controls(self):
        self.run_ps(r'''
$First=Enter-ServiceControlLock $Project 'listener'
try {
    $Rejected=$false
    try { $Second=Enter-ServiceControlLock $Project 'listener' -TimeoutSeconds 0; $Second.Dispose() }
    catch { $Rejected=$true }
    if(-not $Rejected){throw 'Concurrent lock was acquired'}
} finally {$First.Dispose()}
''')

    def test_explicit_stop_repairs_corrupt_intent_without_starting_anything(self):
        (self.recovery / "api.intent.json").write_text("invalid json")
        self.run_ps(r'''
function Get-Process { return $null }
& (Join-Path $Project 'scripts\windows\stop_zhuorui_listener.ps1')
''')
        self.assertFalse(json.loads((self.recovery / "api.intent.json").read_text())["desired_running"])

    def test_healthy_run_resets_backoff_only_after_ten_minutes(self):
        self.run_ps(r'''
function Get-RecoveryObservation { param($ProjectRoot,$Component); if($Component -eq 'monitor'){return @{state='stopped'}}; return @{state='stopped'} }
function Invoke-RecoveryLaunch { throw 'fixture failed once' }
Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:00:00Z')
function Get-RecoveryObservation { param($ProjectRoot,$Component); if($Component -eq 'monitor'){return @{state='stopped'}}; return @{state='running';healthy=$true;pid=999991;started_utc='2026-09-21T00:01:00Z'} }
Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:01:00Z')
Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:10:00Z')
$Before=Get-Content (Join-Path $Project 'runtime\recovery\status.json') -Raw | ConvertFrom-Json
if($Before.components.api.failures -ne 1){throw 'Backoff reset too soon'}
Invoke-RecoveryTick $Project ([datetime]'2026-09-21T00:11:00Z')
''')
        self.assertEqual(self.status()["failures"], 0)
        self.assertIsNone(self.status()["next_attempt_at"])

    def test_interrupted_manual_stop_is_finished_by_watchdog(self):
        self.intent("api", False)
        self.run_ps(r'''
function Get-RecoveryObservation { param($ProjectRoot,$Component); if($Component -eq 'monitor'){return @{state='stopped'}}; return @{state='running';healthy=$true} }
function Request-RecoveryStop { Set-Content (Join-Path $Project 'finished-stop') 'yes' }
function Invoke-RecoveryLaunch { throw 'Do not start a paused process' }
Invoke-RecoveryTick $Project
''')
        self.assertTrue((self.root / "finished-stop").exists())
        self.assertEqual(self.status()["state"], "paused")

    def test_disabled_watchdog_does_nothing(self):
        self.write("settings.json", {"version": 1, "enabled": False})
        self.run_ps("Invoke-RecoveryTick $Project")
        self.assertFalse((self.recovery / "status.json").exists())

    def test_public_status_exposes_stale_watchdog_and_excludes_launch_settings(self):
        self.run_ps(r'''
function Get-RecoveryObservation { return @{state='stopped'; healthy=$false} }
function Invoke-RecoveryLaunch {}
Invoke-RecoveryTick $Project ([datetime]'2020-01-01T00:00:00Z')
''')
        status = recovery_status(self.root)
        self.assertEqual(status["state"], "stale")
        self.assertTrue(status["boot_enabled"])
        self.assertNotIn("ConfigPath", json.dumps(status))
        self.assertNotIn(str(self.root), json.dumps(status))

    def test_installer_validates_context_before_registering_boot_and_minute_triggers(self):
        python = self.root / "runtime" / "python-env" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
        self.run_ps(r'''
Import-Module ScheduledTasks
function Get-ScheduledTask { return $null }
function Register-ScheduledTask {
    param($TaskName,$InputObject,$User,$Password,[switch]$Force)
    if(-not $TaskName.EndsWith(' validation')) {
        if(-not(Test-Path (Join-Path $Project 'runtime\recovery\context-validation.json'))){throw 'Main task installed before validation'}
        @{logon=[int]$InputObject.Principal.LogonType;boot_delay=$InputObject.Triggers[0].Delay;repeat=$InputObject.Triggers[1].Repetition.Interval;duration=$InputObject.Settings.ExecutionTimeLimit;instances=[int]$InputObject.Settings.MultipleInstances;arguments=$InputObject.Actions.Arguments;password_supplied=[bool]$Password} | ConvertTo-Json | Set-Content (Join-Path $Project 'definition.json')
    }
}
function Start-ScheduledTask {
    param($TaskName)
    if($TaskName.EndsWith(' validation')) { @{ok=$true} | ConvertTo-Json | Set-Content (Join-Path $Project 'runtime\recovery\context-validation.json') }
}
function Stop-ScheduledTask {}
function Unregister-ScheduledTask { param($TaskName,[switch]$Confirm) }
function Start-Sleep {}
$UserName=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$TestPassword=New-Object Security.SecureString
foreach($Character in 'synthetic-test-password'.ToCharArray()){$TestPassword.AppendChar($Character)}
$Credential=New-Object Management.Automation.PSCredential($UserName,$TestPassword)
& (Join-Path $Project 'scripts\windows\install_zhuorui_recovery.ps1') -Credential $Credential
''')
        definition = json.loads((self.root / "definition.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(definition["logon"], 1)  # Password, not interactive or S4U.
        self.assertEqual(definition["boot_delay"], "PT45S")
        self.assertEqual(definition["repeat"], "PT1M")
        self.assertEqual(definition["duration"], "PT0S")
        self.assertEqual(definition["instances"], 2)  # IgnoreNew.
        self.assertIn("-WindowStyle Hidden", definition["arguments"])
        self.assertNotIn("synthetic-test-password", json.dumps(definition))
        self.assertTrue(definition["password_supplied"])
        self.assertEqual(json.loads((self.recovery / "settings.json").read_text())["logon_type"], "Password")


if __name__ == "__main__":
    unittest.main()
