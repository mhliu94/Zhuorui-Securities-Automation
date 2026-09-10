# Linux emulator listener and Control Room

Linux uses the existing Android UI trading implementation. The direct API
runtime stores sessions with Windows DPAPI and currently requires Windows.
On Linux, the Control Room selects the UI backend and its Bash launchers.

Create `.venv` with Python and install `requirements.txt`. Configure the shared
`zhuorui_config.json` with Linux Android SDK paths, account credentials and Kafka
settings. Typical SDK executables are `$HOME/Android/Sdk/platform-tools/adb` and
`$HOME/Android/Sdk/emulator/emulator`; tools on `PATH` are also discovered.
Hardware acceleration requires working KVM access. Start the emulator and sign
in before starting the UI listener.

```bash
./start_zhuorui_listener.sh --config ./zhuorui_config.json
./check_zhuorui_listener.sh
./start_zhuorui_monitor.sh --open-browser
./check_zhuorui_monitor.sh
./stop_zhuorui_listener.sh
./stop_zhuorui_monitor.sh
```

The monitor defaults to HTTPS port 443 with HTTP port 80 redirecting to HTTPS.
These ports require root or `CAP_NET_BIND_SERVICE`. Linux launcher options include
`--port`, `--host-address`, `--public-host`, `--interval`, `--certificate-path`
and `--private-key-path`. The dashboard authentication and certificate guidance
in [the monitor guide](monitor.md) also applies.

```bash
sudo ./enable_zhuorui_monitor_firewall.sh
sudo ./trust_zhuorui_monitor_certificate.sh
```

The firewall helper supports UFW and firewalld. Certificate trust supports
Debian/Ubuntu `update-ca-certificates`, Red Hat-family `update-ca-trust`, and
p11-kit; browser-specific certificate stores may need a separate import.
Public certificates can be supplied through the launcher options above.

Rebuild the Android helper with `android/build_zero_idle_dump.sh` when needed.
It requires an Android SDK platform, build-tools, a JDK and JUnit 4. Linux UI
orders retain the legacy UI behavior; native API Market orders and scheduled
API login recovery are described separately in [the API guide](api.md).
