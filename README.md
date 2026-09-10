# Zhuorui Securities Automation

This project includes an authenticated control room for the Zhuorui trading listener and its Android emulator on Windows and Linux.

## Control Room

Start the dashboard from PowerShell:

```powershell
.\start_zhuorui_monitor.ps1 -OpenBrowser
```

On Linux, use the matching Bash launcher:

```bash
./start_zhuorui_monitor.sh --open-browser
```

The dashboard opens at `https://localhost/`, listens on standard HTTPS port 443, and checks the listener and emulator every 60 seconds. HTTP port 80 redirects browsers to HTTPS. Sign in with the single configured administrator account:

- Username: `admin`
- Password: `admin12345`

It shows:

- whether the Zhuorui listener is running;
- its PID, start time, and live run duration;
- the current listener session's last 10 completed holdings-query timings, including average, fastest, and slowest;
- the configured Android virtual device and ADB connection state;
- the emulator start time and live run duration;
- five emulator-stress signals with a Healthy, Under load, or Restart recommended level: machine CPU, machine memory, Android memory pressure, ADB health, and Android response time;
- controls to start or stop the listener and emulator.

Use **Check now** for an immediate status refresh. Stop the dashboard itself with:

```powershell
.\stop_zhuorui_monitor.ps1
```

```bash
./stop_zhuorui_monitor.sh
```

You can check it from PowerShell without opening a browser:

```powershell
.\check_zhuorui_monitor.ps1
```

```bash
./check_zhuorui_monitor.sh
```

The server uses HTTPS, secure server-side sessions, CSRF protection, and login rate limiting. The administrator password is stored in the source only as a salted PBKDF2 hash. Trading account credentials are never sent to the browser.

## External access

The launcher binds to `0.0.0.0` by default. Open the Windows Firewall ports once from an elevated PowerShell window:

```powershell
.\enable_zhuorui_monitor_firewall.ps1
```

On Linux, the firewall helper supports UFW and firewalld:

```bash
sudo ./enable_zhuorui_monitor_firewall.sh
```

The launcher detects the machine's active IPv4 address and uses it for the external URL and HTTP-to-HTTPS redirects. If automatic detection is unavailable, set `public_host` in `zhuorui_config.json`. A router, cloud security group, or upstream network firewall may also need to allow TCP ports 80 and 443.

The included setup creates a self-signed certificate automatically. Browsers will show a certificate warning until the certificate is trusted on the client or replaced with a public certificate for a DNS name. To use a public certificate, pass its PEM files with `-CertificatePath` and `-PrivateKeyPath`.

Trust the generated certificate for browsers on the server by running this from an elevated PowerShell window:

```powershell
.\trust_zhuorui_monitor_certificate.ps1
```

On Linux, add it to the system trust store with:

```bash
sudo ./trust_zhuorui_monitor_certificate.sh
```

The Linux helper supports Debian/Ubuntu `update-ca-certificates`, Red Hat-family
`update-ca-trust`, and p11-kit. Browser-specific certificate stores may still
need a separate import.

Each remote client must also trust `certs\zhuorui-monitor-cert.cer`, otherwise its browser will continue to reject the self-signed certificate.

## Configuration

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

`emulator` may optionally be set to the full path of the platform's emulator executable (`emulator.exe` on Windows or `emulator` on Linux). When omitted, the dashboard derives it from the configured ADB path. `emulator_accel` accepts `auto`, `on`, or `off`; `on` requires an available hardware accelerator and refuses to fall back to software emulation. On Windows, the emulator uses WHPX when it is the installed accelerator.

On Linux, use paths such as `$HOME/Android/Sdk/platform-tools/adb` and
`$HOME/Android/Sdk/emulator/emulator`; hardware acceleration requires working
KVM access. If the SDK tools are on `PATH`, the Python programs discover them
automatically.

For normal operation, start the emulator first and wait for **Running**, then start the listener. Stopping the emulator while the listener is running will interrupt Android automation.

While the dashboard is running, it checks every 30 seconds for a scheduled restart window from 8:01 PM through 9:00 PM America/New_York time. If the last automatic or Web UI emulator start attempt was more than one hour ago, it stops the listener, stops the emulator, waits one minute, starts the emulator, waits two minutes, and makes up to five attempts to foreground Zhuorui before restarting the listener. A failed foreground check stops the emulator again. The restart-attempt time is persisted even when the restart fails.

## Direct server options

The PowerShell launcher accepts `-Port`, `-HostAddress`, `-PublicHost`, `-Interval`, `-CertificatePath`, and `-PrivateKeyPath`. The Python server has matching options. Omit the public-host option to use automatic detection with the configuration fallback:

```powershell
.\.venv\Scripts\python.exe .\zhuorui_monitor.py --host 0.0.0.0 --port 443 --redirect-http-port 80 --interval 60 --cert-file .\certs\zhuorui-monitor-cert.pem --key-file .\certs\zhuorui-monitor-key.pem
```

The Linux launcher exposes the corresponding long options (`--port`,
`--host-address`, `--public-host`, `--interval`, `--certificate-path`, and
`--private-key-path`). Direct invocation looks like:

```bash
./.venv/bin/python ./zhuorui_monitor.py --host 0.0.0.0 --port 443 --redirect-http-port 80 --interval 60 --cert-file ./certs/zhuorui-monitor-cert.pem --key-file ./certs/zhuorui-monitor-key.pem
```

Ports below 1024 require root or the `CAP_NET_BIND_SERVICE` capability on Linux.
The listener can be managed directly with `start_zhuorui_listener.sh`,
`check_zhuorui_listener.sh`, and `stop_zhuorui_listener.sh`. The Android helper
JAR can be rebuilt with `android/build_zero_idle_dump.sh`; it needs an Android
SDK platform, build-tools, a JDK, and JUnit 4.

No additional Python packages are required for the dashboard.
