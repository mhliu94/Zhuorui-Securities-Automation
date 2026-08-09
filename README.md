# Zhuorui Securities Automation

This project includes an authenticated Windows control room for the Zhuorui trading listener and its Android emulator.

## Control Room

Start the dashboard from PowerShell:

```powershell
.\start_zhuorui_monitor.ps1 -OpenBrowser
```

The dashboard opens at `https://localhost/`, listens on standard HTTPS port 443, and checks the listener and emulator every 60 seconds. HTTP port 80 redirects browsers to HTTPS. Sign in with the single configured administrator account:

- Username: `admin`
- Password: `admin12345`

It shows:

- whether the Zhuorui listener is running;
- its PID, start time, and live run duration;
- the current listener session's last 10 completed holdings-query timings, including average, fastest, and slowest;
- the configured Android virtual device and ADB connection state;
- five emulator-stress signals with a Healthy, Under load, or Restart recommended level: machine CPU, machine memory, Android memory pressure, ADB health, and Android response time;
- controls to start or stop the listener and emulator.

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

The dashboard reuses `zhuorui_config.json`. These fields control the emulator integration and the optional public-host fallback:

```json
{
  "adb": "C:\\Users\\Administrator\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe",
  "device": "emulator-5554",
  "avd": "Pixel_10_2",
  "public_host": "dashboard.example.com"
}
```

`emulator` may optionally be set to the full path of `emulator.exe`. When omitted, the dashboard derives it from the configured ADB path.

For normal operation, start the emulator first and wait for **Running**, then start the listener. Stopping the emulator while the listener is running will interrupt Android automation.

## Direct server options

The PowerShell launcher accepts `-Port`, `-HostAddress`, `-PublicHost`, `-Interval`, `-CertificatePath`, and `-PrivateKeyPath`. The Python server has matching options. Omit the public-host option to use automatic detection with the configuration fallback:

```powershell
.\.venv\Scripts\python.exe .\zhuorui_monitor.py --host 0.0.0.0 --port 443 --redirect-http-port 80 --interval 60 --cert-file .\certs\zhuorui-monitor-cert.pem --key-file .\certs\zhuorui-monitor-key.pem
```

No additional Python packages are required for the dashboard.
