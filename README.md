# Zhuorui Securities Automation

Windows tools for direct HTTP API trading, Kafka account publishing and a web
Control Room, with Linux support for the retained emulator/UI listener. Both
implementations read the same `zhuorui_config.json`. See [Linux UI setup](docs/linux-ui.md).

| Component | Current capability |
| --- | --- |
| UI listener | Existing Kafka trading workflow, holdings and emulator login handling. |
| API CLI | Import an app session, check authentication, query holdings, cash and today's orders. |
| API login recovery | Re-login directly with the imported device identity, using the shared login credentials and Beijing-time delay policy. |
| API trading listener | Kafka Buy/Sell Market, Limit, one-second timed-cancel and cancellation commands. |
| API publications | Account details every 30 seconds, plus a queued publication after every order submission attempt and cancellation attempt. |
| Control Room | Starts, stops and restarts the API listener; reports trading mode and publication status. |

## Project layout

```text
zhuorui/
  common/config.py         Shared configuration and credential lookup
  ui/automation.py         Existing emulator trading implementation
  api/                     HTTP client, Kafka listener, execution, journal and sessions
  capture/config.py        Portable emulator/capture settings and boot validation
  monitor/server.py        Control Room server
scripts/windows/           Windows process, HTTPS and firewall helpers
tests/                     UI, monitor and API runtime tests
docs/                      Operating guides and architecture
monitor_web/               Dashboard static assets
android/                   Android hierarchy-dump helper
api_research/              Capture tools, protocol evidence and simulations
  private/                 Captures, APK, CA keys, emulator backups; ignored
runtime/                   Encrypted API session, command journal and state; ignored
logs/                      Process logs
zhuorui_config.json        Existing private configuration; ignored
zhuorui_config.example.json
zhuorui_api.py             API entry point
zhuorui_market_order.py    UI compatibility entry point
zhuorui_monitor.py         Dashboard compatibility entry point
```

Root PowerShell launchers forward to `scripts/windows`, preserving existing
commands and dashboard controls. Configuration, logs, emulator data and dashboard
assets retain their existing paths. See [architecture](docs/architecture.md).

## Run the API script

Use PowerShell from the project directory. Import and run sessions under the same
Windows user. Create a separate API environment:

```powershell
py -m venv .venv-api
.\.venv-api\Scripts\python.exe -m pip install -r requirements-api.txt
```

Use the existing `zhuorui_config.json`. For a new checkout only, copy the example
to that name and fill in its settings. The optional `api` section has defaults.
Do not overwrite a private config with
the example.

Check local setup without network requests:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py status
```

Read the running emulator's identity, then import its existing login directly:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py inspect-emulator
.\.venv-api\Scripts\python.exe .\zhuorui_api.py import-emulator-session
.\.venv-api\Scripts\python.exe .\zhuorui_api.py check-session
```

Direct import needs **root read access** on the selected emulator and the
validated Zhuorui 3.1.5 build. It reads the broker user ID, token, app-specific
device ID and headers automatically. It never performs a new login or enables
root. The current temporary debug boot supports it; a normal Google Play emulator
boot generally does not. The API environment alone is sufficient for this import.

For another app build or an emulator without direct storage access, the existing
capture importer remains available. With working capture on the **same logged-in
emulator**, refresh holdings or Today's Orders, then import within ten minutes:

```powershell
.\api_research\.venv\Scripts\python.exe .\zhuorui_api.py import-session
```

Both importers encrypt the existing session with Windows DPAPI. Local import is
not proof of server validity; `check-session` checks that separately. See
[API setup and recovery](docs/api.md) and [another Windows machine/account](docs/windows-porting.md).

Run direct account queries:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py check-session
.\.venv-api\Scripts\python.exe .\zhuorui_api.py holdings
.\.venv-api\Scripts\python.exe .\zhuorui_api.py cash
.\.venv-api\Scripts\python.exe .\zhuorui_api.py orders
```

Direct query results contain the broker's raw private account fields. The Kafka
listener maps account, cash and holdings reads to the existing KTrader snapshot
format. Normal API operations use the encrypted session and HTTPS without UI
clicks, proxy or emulator calls. After the initial session import, routine API
password re-login also works without the emulator running. The listener uses the
existing `trade_password` setting to unlock trading on demand before dispatching
an order or cancellation, then verifies the account's authorization. Keep access
to the emulator for phone verification or a manual session import.

The listener enables automatic login recovery by default through
`api.auto_login_enabled`. It reuses `login.phone`, `login.password` and
`login.phone_area` (default `"86"`) with the imported account/device identity.
Logout first detected from **09:00 inclusive to 16:00 exclusive Beijing time**
waits five minutes from detection; outside that interval, recovery is immediately
eligible at the next command boundary. Duplicate errors and listener restarts do
not reset that deadline. Verification or explicit login rejection pauses attempts;
temporary connection failures retry after `api.login_retry_seconds` (default 300).
Recovery never replays a trade. See [session recovery](docs/api.md#login-recovery)
for configuration and manual recovery.

Append `--config C:\path\to\config.json` after any command for another shared
config. `python -m zhuorui.api <command>` is the equivalent package entry point.

Inspect order formats offline:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py plan-order BILI buy 1 --type market
.\.venv-api\Scripts\python.exe .\zhuorui_api.py plan-order BILI buy 1 --type limit --price 25.1000
.\.venv-api\Scripts\python.exe .\zhuorui_api.py plan-order BILI buy 1 --type timed-cancel --price 25.1000
.\.venv-api\Scripts\python.exe .\zhuorui_api.py plan-cancel EXAMPLE_ORDER_REFERENCE
```

These are synthetic plans and send nothing. Market uses native `MO` without a
limit price. Limit prices round to cents: Buy rounds up and Sell rounds down.
Limit orders default to regular plus pre/post trading (`allowPrePost: "Y"`);
`--no-allow-pre-post` selects regular hours only for an offline plan, and Kafka
commands can select it with `allow_pre_post: false`. Native Market orders retain
the captured regular-hours format. Buy and Sell are supported.
Timed-cancel uses Limit with a one-second
deadline from dispatch; it is not native FOK and permits partial fills. A late
acknowledgement triggers cancellation when the reference becomes available;
network delays can miss the target.

## Start the API listener

```powershell
.\start_zhuorui_listener.ps1
.\check_zhuorui_listener.ps1
.\stop_zhuorui_listener.ps1
```

The launchers now default to API and use `.venv-api`, or another Python configured
through `api.python_executable`. For foreground operation instead:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py listen
```

**Trading is enabled by default.** Both `api.live_orders_enabled` and the shared
account `trading_enabled` switch default to `true`. An explicit `false` on either
effective switch disables execution. For publishing-only operation, set
`api.live_orders_enabled` to `false`; the listener still validates commands and
publishes account details with `trading_enabled: false`. Configuration changes
take effect on restart. Dashboard controls preserve the configured mode.

The shared `kafka` section supplies the control-server address and topics. Set
`kafka.holdings_interval_seconds` to `30`; this is also the API default. Each
submission attempt schedules a fresh holdings read two seconds after it completes
on an independent worker. This does not delay the next order or FOK cancellation,
or reset the 30-second periodic schedule. Cancellation attempts also trigger a
refresh. Existing in-flight publications and broker or Kafka outages can delay
delivery.

Market, Limit and FOK commands each wait five seconds before submission, one
command at a time. Two queued orders therefore accumulate five and ten seconds
of intentional waiting, plus normal processing time. The FOK cancellation timer
starts at dispatch, after this wait. Cancellation commands are not delayed.

Every command needs a matching `account_id` or `account_num_id`. Provided server
selectors must match too. `MARKET_ORDER` rejects price fields and uses native `MO`
during regular hours. In premarket and after-hours it becomes a DAY `LO` using
Zhuorui's fresh order book: best ask +1% for buys (round up to cents), best bid
-1% for sells (round down). A failed or unusable price read is retried once;
orders are never resubmitted automatically. Closed or unknown sessions reject.
`LIMIT_ORDER` and `LIMIT_ORDER_FOK` require `qty_shares` and `limit_price`; FOK uses
the one-second cancellation policy. Market also accepts `notional_usd` when a
fresh real-time quote is available; delayed quotes require explicit `qty_shares`.

Producer command IDs or stable Kafka coordinates prevent duplicate submission.
Commands older than 120 seconds are rejected by default. An unknown submission
pauses new execution for reconciliation and is never blindly retried. See
[the API guide](docs/api.md) for routing, cancellation and recovery details.

## Run the existing UI listener

Install UI dependencies in `.venv` if needed:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the configured emulator and sign in, then use:

```powershell
.\start_zhuorui_listener.ps1 -Backend ui
.\check_zhuorui_listener.ps1 -Backend ui
.\stop_zhuorui_listener.ps1 -Backend ui
```

The explicit `-Backend ui` option runs the retained UI implementation. Launchers
refuse simultaneous API and UI listeners. Its Market command still uses the
price-adjusted Limit approach; its
FOK command still uses the three-second Revoke behavior. The API requirements
are different, as described above.

## Control Room

```powershell
.\start_zhuorui_monitor.ps1 -OpenBrowser
```

The dashboard defaults to API listener controls and reports its actual trading
mode. Scheduled emulator restarts are disabled in API mode; restarting the API
listener preserves the emulator login. See
[the dashboard guide](docs/monitor.md) for login, HTTPS, remote access and options.

## Tests

UI and dashboard regression tests:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_zhuorui_market_order tests.test_zhuorui_monitor
```

API runtime tests use synthetic sessions and mocked HTTP responses:

```powershell
.\.venv-api\Scripts\python.exe -m unittest discover -s tests -p 'test_api*.py'
.\.venv-api\Scripts\python.exe -m unittest tests.test_emulator_session tests.test_capture_config
```

Research simulations and signing checks:

```powershell
$env:PYTHONPATH = "$PWD;$PWD\api_research"
.\api_research\.venv\Scripts\python.exe -m unittest discover -s api_research -p 'test_*.py'
```

No test submits or cancels a real trade. [Protocol evidence](api_research/README.md)
distinguishes observed formats and verified login recovery from unverified trade execution.
