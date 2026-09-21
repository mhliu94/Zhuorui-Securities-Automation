# Control Room operating guide

Return to the [project README](../README.md) for the API and UI entry points.
The dashboard controls the API listener. The Android emulator is managed separately as the source of an existing login session.

This project includes an authenticated Windows control room for API trading, Kafka account publications, and the session emulator.

On Linux, the same Control Room manages the retained UI listener through Bash
helpers. See [Linux UI setup](linux-ui.md) for its emulator and launch requirements.

## Control Room

Start the dashboard from PowerShell:

```powershell
.\start_zhuorui_monitor.ps1 -OpenBrowser
```

The dashboard opens at `https://localhost/`, listens on standard HTTPS port 443, and checks the listener and emulator every 60 seconds. HTTP port 80 redirects browsers to HTTPS. Sign in with the single configured administrator account:

- Username: `admin`
- Password: `admin12345`

It shows:

- whether the Zhuorui API listener is running;
- its PID, start time, and live run duration;
- its effective order mode: **Validation only** or **Live orders enabled**;
- the latest confirmed holdings publication time and current listener errors;
- the current listener session's last 10 completed holdings-query timings, including average, fastest, and slowest;
- the configured Android virtual device and ADB connection state;
- the emulator start time and live run duration;
- Windows CPU/memory and three separate emulator readings: Android memory pressure, ADB health, and Android response time; overall API resource health uses Windows readings;
- controls to start, stop, or restart the API listener, plus separate manual emulator start/stop controls.

**Start API** launches `zhuorui_api.py listen` using `.venv-api\Scripts\python.exe`, or `api.python_executable` when configured. It consumes Kafka commands and publishes holdings on startup, every 30 seconds, and immediately after each new order submission. Monitor checks remain every 60 seconds; the holdings schedule runs independently.

**Stop API** asks the listener to finish active work, including timed cancellation and queued holdings publications. **Restart API** waits for that stop to succeed before starting a new listener. Neither control restarts the emulator. If stopping takes more than 30 seconds, the dashboard reports that work is still finishing; the stop request remains active and the process is not force-killed. Check status again before starting it.

Starting or restarting the service uses the configured order mode. The API combines `api.live_orders_enabled` with the shared account `trading_enabled` switch; both default to true and explicit disabled settings are preserved. Configuration changes take effect on listener restart; the running dashboard reports the effective mode from that listener's status.

Use **Check now** for an immediate status refresh. Stop the dashboard itself with:

```powershell
.\stop_zhuorui_monitor.ps1
```

You can check it from PowerShell without opening a browser:

```powershell
.\check_zhuorui_monitor.ps1
```

The server uses HTTPS, secure server-side sessions, CSRF protection, and login rate limiting. The administrator password is stored in the source only as a salted PBKDF2 hash. Trading account credentials are never sent to the browser.

## External access

The launcher binds to `0.0.0.0` by default. Open the Windows Firewall ports once from an elevated PowerShell window:

```powershell
.\enable_zhuorui_monitor_firewall.ps1
```

The launcher detects the machine's active IPv4 address and uses it for the external URL and HTTP-to-HTTPS redirects. If automatic detection is unavailable, set `public_host` in `zhuorui_config.json`. A router, cloud security group, or upstream network firewall may also need to allow TCP ports 80 and 443.

The included setup creates a self-signed certificate automatically. Browsers will show a certificate warning until the certificate is trusted on the client or replaced with a public certificate for a DNS name. To use a public certificate, pass its PEM files with `-CertificatePath` and `-PrivateKeyPath`.

Trust the generated certificate for browsers on the server by running this from an elevated PowerShell window:

```powershell
.\trust_zhuorui_monitor_certificate.ps1
```

Each remote client must also trust `certs\zhuorui-monitor-cert.cer`, otherwise its browser will continue to reject the self-signed certificate.

## Configuration

For the **Restart protection** panel, unattended startup, persistent Stop behavior,
and watchdog setup, see [Windows recovery](windows-recovery.md).

The dashboard reuses `zhuorui_config.json`. These fields control the emulator integration and the optional public-host fallback:

```json
{
  "adb": "C:\\Users\\Administrator\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe",
  "device": "emulator-5554",
  "avd": "Pixel_10_2",
  "emulator_accel": "on",
  "public_host": "dashboard.example.com"
}
```

`emulator` may optionally be set to the full path of `emulator.exe`. When omitted, the dashboard derives it from the configured ADB path. `emulator_accel` accepts `auto`, `on`, or `off`; `on` requires an available hardware accelerator and refuses to fall back to software emulation. On Windows, the emulator uses WHPX when it is the installed accelerator.

Import the existing emulator login as described in the [API guide](api.md), then start the API listener. Normal API commands use the saved session. The emulator is needed again when importing a new login; it does not need to restart when the API listener restarts.

The API dashboard does **not** run the old nightly emulator restart schedule. Emulator controls are manual.

The listener controls are also available from PowerShell:

```powershell
.\start_zhuorui_listener.ps1
.\check_zhuorui_listener.ps1
.\stop_zhuorui_listener.ps1
```

These commands default to the API backend. Explicit `-Backend ui` selects the legacy emulator listener; the dashboard continues to control only the API listener. The launchers refuse to start both backends at the same time. API process metadata is stored in `zhuorui_api_listener.pid` and `zhuorui_api_listener.current.json`, separately from legacy metadata.

Optional `api.state_file` and `api.stop_file` paths default to `runtime/api/listener-state.json` and `runtime/api/listener.stop` under the checkout. Explicit relative paths, including `api.python_executable`, are resolved against the configuration file directory. Use the same Windows user that imported the encrypted broker session.

## Direct server options

The PowerShell launcher accepts `-Port`, `-HostAddress`, `-PublicHost`, `-Interval`, `-CertificatePath`, and `-PrivateKeyPath`. The Python server has matching options. Omit the public-host option to use automatic detection with the configuration fallback:

```powershell
.\.venv\Scripts\python.exe .\zhuorui_monitor.py --host 0.0.0.0 --port 443 --redirect-http-port 80 --interval 60 --cert-file .\certs\zhuorui-monitor-cert.pem --key-file .\certs\zhuorui-monitor-key.pem
```

No additional Python packages are required for the dashboard.
