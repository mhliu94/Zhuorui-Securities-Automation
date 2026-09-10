"""Exercise Windows launch/stop behavior using fake processes in a temporary project."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("powershell.exe")
SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "windows"


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class ListenerLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.scripts = self.root / "scripts" / "windows"
        self.scripts.mkdir(parents=True)
        for name in ("listener_common.ps1", "start_zhuorui_listener.ps1", "stop_zhuorui_listener.ps1"):
            shutil.copyfile(SOURCE / name, self.scripts / name)
        self.python = self.root / ".venv-api" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"")
        (self.root / "zhuorui_api.py").write_text("", encoding="utf-8")
        (self.root / "zhuorui_config.json").write_text(json.dumps({"api": {"live_orders_enabled": False}}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, content):
        path = self.root / "test.ps1"
        path.write_text("$ErrorActionPreference = 'Stop'\n" + content, encoding="utf-8")
        return subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            capture_output=True, text=True, timeout=10,
        )

    def test_start_defaults_to_api_and_preserves_explicit_disabled_override(self):
        result = self.run_script(r"""
function Start-Process {
    param($WindowStyle, $WorkingDirectory, $FilePath, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
    @{ executable = $FilePath; arguments = $ArgumentList; window = $WindowStyle } | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $WorkingDirectory 'spawn.json')
    $Fake = [pscustomobject]@{ Id = 999991; StartTime = [datetime]::UtcNow; HasExited = $false }
    $Fake | Add-Member ScriptMethod Refresh {}
    return $Fake
}
function Get-Process { return $null }
function Start-Sleep {}
& (Join-Path $PSScriptRoot 'scripts\windows\start_zhuorui_listener.ps1')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        spawn = json.loads((self.root / "spawn.json").read_text(encoding="utf-8-sig"))
        metadata = json.loads((self.root / "zhuorui_api_listener.current.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(spawn["executable"], str(self.python))
        self.assertIn("listen", spawn["arguments"])
        self.assertNotIn("server", spawn["arguments"])
        self.assertEqual(spawn["window"], "Hidden")
        self.assertEqual(metadata["backend"], "api")
        self.assertFalse(metadata["live_orders_enabled"])
        self.assertEqual(metadata["stop_file"], str(self.root / "runtime" / "api" / "listener.stop"))

    def test_start_requires_explicit_api_runtime_when_default_missing(self):
        self.python.unlink()
        result = self.run_script(r"""
function Start-Process { throw 'Unexpected launch' }
& (Join-Path $PSScriptRoot 'scripts\windows\start_zhuorui_listener.ps1')
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("api.python_executable", result.stderr)
        self.assertFalse((self.root / "zhuorui_api_listener.pid").exists())

    def test_order_mode_matches_runtime_account_override(self):
        result = self.run_script(r"""
. (Join-Path $PSScriptRoot 'scripts\windows\listener_common.ps1')
$Config = @{}
if (-not (Get-ApiListenerOrderMode -Config $Config)) { throw 'Default order mode should be enabled' }
$Config = @{ api = @{ live_orders_enabled = $false } }
if (Get-ApiListenerOrderMode -Config $Config) { throw 'Explicit disabled override ignored' }
$Config = @{ api = @{ live_orders_enabled = 'true' }; trading_enabled = $true; account = @{ trading_enabled = $false } }
if (Get-ApiListenerOrderMode -Config $Config) { throw 'Account disabled mode ignored' }
$Config.trading_enabled = $false
$Config.account.trading_enabled = 'yes'
if (-not (Get-ApiListenerOrderMode -Config $Config)) { throw 'Account override did not match shared configuration' }
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_runtime_and_stop_paths_resolve_relative_to_config(self):
        config_dir = self.root / "portable"
        config_dir.mkdir()
        runtime = config_dir / "python.exe"
        runtime.write_bytes(b"")
        (config_dir / "account.json").write_text(json.dumps({"api": {
            "python_executable": "python.exe", "stop_file": "stop.request", "state_file": "state.json",
        }}), encoding="utf-8")
        result = self.run_script(r"""
function Start-Process {
    $Fake = [pscustomobject]@{ Id = 999991; StartTime = [datetime]::UtcNow; HasExited = $false }
    $Fake | Add-Member ScriptMethod Refresh {}
    return $Fake
}
function Get-Process { return $null }
function Start-Sleep {}
& (Join-Path $PSScriptRoot 'scripts\windows\start_zhuorui_listener.ps1') -ConfigPath 'portable\account.json'
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = json.loads((self.root / "zhuorui_api_listener.current.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(metadata["python"], str(runtime))
        self.assertEqual(metadata["stop_file"], str(config_dir / "stop.request"))
        self.assertEqual(metadata["state_file"], str(config_dir / "state.json"))

    def test_api_stop_requests_graceful_exit_and_never_force_kills(self):
        result = self.run_script(r"""
$Started = [datetime]::UtcNow
$StopPath = Join-Path $PSScriptRoot 'runtime\api\listener.stop'
'999991' | Set-Content (Join-Path $PSScriptRoot 'zhuorui_api_listener.pid')
@{ pid = 999991; backend = 'api'; started_utc = $Started.ToString('o'); script = (Join-Path $PSScriptRoot 'zhuorui_api.py'); stop_file = $StopPath } | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot 'zhuorui_api_listener.current.json')
function Get-Process {
    $Fake = [pscustomobject]@{ Id = 999991; StartTime = $Started }
    $Fake | Add-Member ScriptMethod WaitForExit { param($milliseconds) return $false }
    return $Fake
}
function taskkill.exe { throw 'Unexpected force kill' }
& (Join-Path $PSScriptRoot 'scripts\windows\stop_zhuorui_listener.ps1')
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not force-killed", result.stderr)
        self.assertTrue((self.root / "runtime" / "api" / "listener.stop").exists())
        self.assertTrue((self.root / "zhuorui_api_listener.pid").exists())

    def test_backend_mismatch_does_not_send_stop_request(self):
        result = self.run_script(r"""
$Started = [datetime]::UtcNow
'999991' | Set-Content (Join-Path $PSScriptRoot 'zhuorui_api_listener.pid')
@{ pid = 999991; backend = 'ui'; started_utc = $Started.ToString('o'); script = (Join-Path $PSScriptRoot 'zhuorui_api.py'); stop_file = (Join-Path $PSScriptRoot 'bad.stop') } | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot 'zhuorui_api_listener.current.json')
function Get-Process { return [pscustomobject]@{ Id = 999991; StartTime = $Started } }
function taskkill.exe { throw 'Unexpected force kill' }
& (Join-Path $PSScriptRoot 'scripts\windows\stop_zhuorui_listener.ps1')
""")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unverified", result.stderr)
        self.assertFalse((self.root / "bad.stop").exists())


if __name__ == "__main__":
    unittest.main()
