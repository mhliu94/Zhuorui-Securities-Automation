# Another Windows machine or account

Install the Python requirements and Android SDK/emulator on the new machine.
Use that account's existing logged-in emulator. First-device phone verification
is handled by the user, outside this script.

Copy the project and fill in its private `zhuorui_config.json`. Keep the new
account's credentials, Kafka routing identifiers and broker session separate.
Do not copy another account's `runtime` session, `api_research/private` captures,
emulator backups or proxy CA keys. DPAPI sessions must be imported under the
Windows user that will run the API script.

## Settings Codex can fill in

The public example contains all portable settings. Capture helpers accept
`-ConfigPath C:\path\to\config.json` in PowerShell or
`--config C:\path\to\config.json` in Python. API commands accept `--config`
after the command. Explicit relative paths are relative to the config file.
Do not change capture paths or ports while a capture process is running; finish
the capture with its original config first.

| Setting | How it is determined |
| --- | --- |
| Top-level `adb`, `device`, `avd` | SDK executable, emulator serial and AVD name. `inspect-emulator` discovers a unique running emulator if the latter two are omitted; if supplied, they must match. Capture setup requires both explicitly. |
| `capture.sdk_root`, `emulator_executable` | Normally inferred from `adb`; set explicitly for a custom installation. |
| `capture.avd_home`, `original_avd_dir` | Defaults to Android's AVD home and the AVD registration; custom directories are supported. The directory must match the registered AVD. |
| `capture.system_image_dir` | Inferred from the original AVD's `config.ini`; override for an explicit image location. There is no fixed Android release in the launcher. |
| `capture.test_system_image_dir` | Defaults to the same release/ABI's Google APIs sibling; install it or specify another test image. |
| `capture.private_dir` | Defaults to `api_research/private`; holds captures, CA files, validation and backups. Keep any custom folder private and outside version control. |
| `capture.python_executable`, `mitmdump_executable` | Defaults to the research virtual environment; configurable for another dependency installation. |
| `capture.debug_policy_file`, `debug_ramdisk_file` | Defaults to files under the private directory. The policy and temporary boot image must be validated for the installed Android build. |
| `capture.proxy_listen_host`, `proxy_port` | Local loopback address and free port, defaults `127.0.0.1:8082`. |
| `capture.emulator_proxy_host` | Host address seen by the emulator, normally `10.0.2.2`; it uses `proxy_port`. |
| `capture.test_avd`, `test_port` | Separate disposable capture test, defaults `ZhuoruiCapture`/5580. |
| `capture.boot_test_avd`, `boot_test_port` | Separate empty original-image boot test, defaults `ZhuoruiBootTest`/5582. |
| `capture.boot_timeout_seconds` | Default 180. |
| `api.session_file` | New encrypted cache for this account and Windows user. |
| `login.phone`, `login.password`, `login.phone_area` | Shared account login credentials; country calling code defaults to the string `"86"`. Used for direct same-device password recovery. |
| `api.auto_login_enabled`, `login_retry_seconds` | Automatic password recovery defaults to enabled; transient failure retry defaults to 300 seconds. |
| `api.expected_user_id` | Optional actual broker user ID, read from the selected emulator. This is different from the Kafka `account_id`/`account_num_id`. No manual token entry is needed. |
| `api.capture_file`, `apk_file` | Optional explicit overrides. If omitted, follow `capture.private_dir`. Update explicit overrides too if relocating that directory. |

Null optional capture paths mean “infer the default.” Show the resolved values:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py capture-settings
.\.venv-api\Scripts\python.exe .\zhuorui_api.py inspect-emulator
```

Names and ports of original/test emulators must be distinct; console ports must
be even. Capture setup refuses an unexpected running AVD or mismatched backup.
The temporary debug-boot checks bind validation to source ramdisk, policy and
output hashes. Copying an old validation record does not validate a new image.
Codex can inspect installed images and perform the boot/certificate checks
described in [the capture checklist](../api_research/CAPTURE_CHECKLIST.md).

## Import and verify the account

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py import-emulator-session
.\.venv-api\Scripts\python.exe .\zhuorui_api.py check-session
.\.venv-api\Scripts\python.exe .\zhuorui_api.py holdings
```

Direct import reads the current app session without logging in elsewhere.
It requires root reads and the validated APK build. A normal stock Google Play
boot may need the validated temporary debug setup again. If direct import is
unsupported, use the capture importer after setting up HTTPS capture on the same
logged-in emulator. Installing dependencies alone does not grant root access or
configure certificate trust.

After import, normal API operations use the encrypted cache and HTTPS. The
implemented Kafka listener publishes account snapshots every 30 seconds and
after submission attempts. It supports native Market, Limit and one-second
timed cancellation for Buy/Sell. The default launchers and Control Room manage
this API listener; see [API operation](api.md) for configuration and commands.

Use a separate API consumer group, session and journal for each account. The
default group adds an account suffix; any explicit override must remain unique.
Live execution defaults to enabled: both `api.live_orders_enabled` and the shared
account trading switch default to `true`. Existing explicit disabled settings are
preserved when porting. Set `api.live_orders_enabled` to `false` for publishing-only
operation, which publishes `trading_enabled: false`. Restart after config changes.

After the initial import, routine API operations and password re-login use the
saved device identity without a running emulator. A usable DPAPI cache is
preferred at startup; a logged-out emulator does not replace it. Optional local
automatic import can bootstrap from a valid, root-readable emulator when needed;
this is separate from `api.auto_login_enabled` and defaults to false.

On confirmed logout, automatic password recovery waits five minutes from the
first detection if Beijing time is 09:00 inclusive to 16:00 exclusive. Outside
that interval it is immediately eligible between commands. Repeated detections
and listener restarts preserve the deadline in the recovery sidecar beside the
encrypted cache (`session.recovery.json` for `session.dpapi`). Keep that metadata
with its account; it is not a substitute for importing a session on the new
Windows machine.

Set the new account's login credentials and country calling code correctly.
Phone verification (`010007`) and other explicit login rejections pause automatic
attempts for review; transport failures retry after `api.login_retry_seconds`.
Use the emulator for verification, manual re-import and trading-password unlock.
Trading-password submission remains unimplemented in the API runtime. Successful
fills/cancellations and account-specific logout recovery still need controlled
validation. Recovery never replays an earlier order.
