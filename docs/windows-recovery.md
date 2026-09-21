# Windows reboot and crash recovery

The watchdog runs independently through Windows Task Scheduler. A boot trigger
waits 45 seconds; a repeating trigger checks both the API listener and Control
Room every minute. An initial restart is attempted on the first check after an
exit. Repeated launch failures or short runs use delays of 60, 120, 240, then
300 seconds. Ten minutes of continuous process health resets that backoff.

The watchdog preserves all configured trading switches, login recovery state,
command age limits, and the durable order journal. It sends no trading commands
itself. Restarting does not replay orders or resolve uncertain submissions.

## Install

Run setup under the Windows account that owns the encrypted API session.
First create an independent Python runtime from an existing working CPython
installation. The example uses the current API environment to identify its base:

```powershell
.\.venv-api\Scripts\python.exe scripts\windows\prepare_runtime.py --source-python .\.venv-api\Scripts\python.exe
```

This copies the interpreter and standard library into `runtime/python-base`,
creates a fresh environment in `runtime/python-env`, and installs
`requirements-api.txt`. It does not move or overwrite the existing environments.
The new environment is preferred by API and monitor launchers. An explicit
`api.python_executable` still takes precedence and must also use an independent
installation for unattended recovery. Keep this Python installation updated
separately from Codex; copying it does not provide automatic Python updates.

Use the updated Start or Stop controls once for each component to save the
desired state and launch parameters. Starting an already running component
records its requested state without launching another copy. For an existing
installation, perform a graceful API stop/start and a monitor stop/start to
adopt the new Python runtime. The legacy monitor supports a one-time manual
stop using its old process-tree termination; newly launched monitors stop
gracefully through a stop file.

```powershell
.\start_zhuorui_listener.ps1
.\start_zhuorui_monitor.ps1
.\install_zhuorui_recovery.ps1
```

The installer opens a native Windows credential dialog for the **current Windows
account password**. Do not enter a broker password. Windows Task Scheduler stores
the credential; it is not written into project files, logs, or task arguments.
The task uses password logon so it can run before desktop sign-in. The installer
first runs a separate read-only task to verify session decryption, independent
Python, and absence of Codex package identity under that actual Windows logon.
Failure leaves automatic recovery unenabled.

For explicitly limited operation while the user is signed in, use
`-InteractiveOnly`. The monitor identifies this limitation. This option cannot
recover after reboot until that user signs in. S4U and SYSTEM are deliberately
not substituted for the account that owns the DPAPI session.

Relevant Windows documentation:
[task logon types](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type),
[DPAPI user scope](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).

## Start, Stop, and recovery

- **Stop** saves a persistent paused state before requesting process shutdown.
  It works even if the component already crashed. It stays paused across reboot.
- **Start** saves the desired running state and resumes recovery. A watchdog
  launch checks that state again while holding the same control lock, so a
  concurrent Stop cannot be overwritten by an old restart decision.
- **Restart API** keeps the existing graceful-stop sequence. If the old process
  has not exited, no replacement is started.
- Manual controls and watchdog launches use cross-session filesystem locks.
  The API journal lock and monitor instance lock also prevent concurrent servers.
- Missing PID files trigger an orphan-process check. Unverified process identities
  require attention; the watchdog does not kill or replace them.
- Login errors, broker outages, and old account publications alone do not trigger
  restarts. The API health check uses fresh process-state updates. The monitor
  health check probes HTTPS on loopback at its saved port.
- Three consecutive unhealthy checks request a graceful stop. API process state
  must first be over three minutes old to count as unhealthy. The watchdog waits
  for confirmed process exit and never force-kills a hung process. A process that
  cannot stop requires manual attention.

Use the managed Start/Stop scripts or dashboard controls for intentional pauses.
A direct process kill is treated as a crash. The watchdog only supervises the API
backend and Control Room; it does not start the Android emulator or UI listener.

## Status, diagnosis, and removal

The Control Room's **Restart protection** panel shows watchdog freshness,
component state, and the next retry when applicable. Overdue watchdog checks
are reported after three minutes. Service transitions and restart attempts are
written to daily `logs/watchdog_YYYYMMDD.log` files. Component logs retain their
startup, error, and shutdown details. Windows Task Scheduler history records
failures to launch the watchdog itself, including invalid Windows credentials.

`runtime/recovery` contains separate intent files, a status snapshot, installation
settings, locks, and the read-only context validation result. Updates are atomic;
unreadable or malformed intent is handled by refusing automatic launches.
These files and the independent Python runtime are ignored by Git.

```powershell
.\uninstall_zhuorui_recovery.ps1
```

Uninstall disables and removes only this project's verified watchdog task. It
does not stop running programs or erase intentional-stop settings or sessions.
If the Windows account password changes, re-run the installer to update its
stored task credential. Verify recovery after planned Windows maintenance.

## Verification

```powershell
.\runtime\python-env\Scripts\python.exe -m unittest tests.test_windows_recovery tests.test_listener_launchers tests.test_zhuorui_monitor
.\runtime\python-env\Scripts\python.exe -m unittest tests.test_windows_recovery_integration
.\runtime\python-env\Scripts\python.exe -m unittest discover -s tests -p 'test_api*.py'
```

Tests use temporary projects and fake broker/process behavior. They cover
persistent pauses, stop/restart races, duplicate controls, retry timing across
watchdog restarts, orphan detection, nonfatal login/network errors, and graceful
handling of hangs. The scheduled-context validation is read-only. A real reboot
test is a separate planned operation: leave services enabled, reboot, avoid
desktop sign-in, then check public HTTPS and account publications externally.
