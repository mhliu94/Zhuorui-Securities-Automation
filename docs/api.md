# Direct API setup and operation

`zhuorui_api.py`, also available as `python -m zhuorui.api`, supports account
reads, offline Buy/Sell order plans and the Kafka API trading listener. Ordinary
API requests use the imported session and HTTPS without emulator UI operations.

## Session import

The runtime and direct emulator importer use `requirements-api.txt`. With the
same logged-in emulator connected and root reads available:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py inspect-emulator
.\.venv-api\Scripts\python.exe .\zhuorui_api.py import-emulator-session
.\.venv-api\Scripts\python.exe .\zhuorui_api.py check-session
```

The importer automatically reads the saved broker user ID and token, app-specific
Android ID, device headers and app signing material. It checks the exact APK
build, current MMKV checksums and account identity, then stores the session under
Windows DPAPI. If the configured APK is missing it copies the verified installed
APK locally. No proxy, password, new login or phone verification is involved.
It neither enables root nor restarts the emulator. Root access is a prerequisite,
including after any normal Google Play emulator restart. Direct import currently
supports the validated 3.1.5 APK on Android 12L or newer, main Android profile only;
the live verification used Android 16. Other builds require capture validation.

`inspect-emulator` reports emulator name/serial and a non-secret account fingerprint,
not the token. These emulator identifiers and the broker's user/device identifiers
are separate. A saved token does not prove the server still accepts it; run
`check-session` after either import. Neither importer performs re-login.

The alternative traffic-capture importer additionally requires the research
environment. If it is not already installed:

```powershell
py -m venv api_research\.venv
.\api_research\.venv\Scripts\python.exe -m pip install -r api_research\requirements.txt
```

Import expects a recent successful account request in the configured local
capture. Use the original-emulator procedure in
[the capture checklist](../api_research/CAPTURE_CHECKLIST.md). Installing Python
packages alone does not configure the emulator certificate or proxy. A restart
removes the temporary capture certificate; verify readable traffic first.

With capture active, refresh holdings or Today's Orders, then run:

```powershell
.\api_research\.venv\Scripts\python.exe .\zhuorui_api.py import-session
```

Import makes no network request. It reads the latest account query, checks
success and freshness, verifies the signing key against its signature, and
imports the existing session and device headers. It will not use an older
successful query to hide a newer failure, or import a token invalidated by a
later captured logout.

The session and signing material are encrypted with Windows DPAPI. Import and
run under the same Windows user. The cache is bound to configured account/server
labels, and re-import rejects a change of broker user or device identity. Local
account labels are not assumed to equal broker identifiers. Optional
`api.expected_user_id` pins the actual broker user, including on the first import.
Without that setting, the first import trusts the selected emulator/capture;
subsequent imports enforce the stored identity. Use a separate config and session
file for another account. See [Windows portability](windows-porting.md).

## Shared configuration

Both implementations use `zhuorui/common/config.py` and the existing private
`zhuorui_config.json`. Optional API settings for a config at the project root:

```json
{
  "api": {
    "live_orders_enabled": true,
    "auto_login_enabled": true,
    "login_retry_seconds": 300,
    "auto_import_session": false,
    "expected_user_id": null,
    "session_file": "runtime/api/session.dpapi",
    "journal_file": "runtime/api/commands.sqlite3",
    "state_file": "runtime/api/listener-state.json",
    "stop_file": "runtime/api/listener.stop",
    "capture_file": "api_research/private/zhuorui-flows.mitm",
    "apk_file": "api_research/private/zhuorui.apk",
    "request_timeout_seconds": 10,
    "session_max_capture_age_seconds": 600,
    "command_max_age_seconds": 120,
    "quote_max_age_seconds": 30,
    "cancel_after_seconds": 1
  },
  "login": {"phone_area": "86"}
}
```

Explicit relative API paths resolve against the config's directory. Omitted
paths point into the checkout. The 600-second setting checks import freshness;
it is **not** an assumed token lifetime. The cancellation setting targets the
deadline from dispatch for the Limit/FOK workflow. `api.python_executable`
optionally overrides the launcher's `.venv-api\Scripts\python.exe`.

Existing credentials, Kafka settings, emulator settings and `trading_enabled`
remain in the shared config. The listener uses the shared Kafka bootstrap
servers and command/holdings/status topics. Set
`kafka.holdings_interval_seconds` to `30`; it also defaults to 30 when omitted.
Automatic login reuses the shared `login.phone` and `login.password` credential
lookup. `login.phone_area` is the calling-code string, default `"86"`; set it
for the account being moved to this machine.

API execution requires both `api.live_orders_enabled` and the shared account
`trading_enabled` switch. Both default to `true`; explicit disabled settings remain
effective. For publishing-only operation, set `api.live_orders_enabled` to `false`.
The listener then receives and validates commands, reports disabled results and
publishes snapshots with `trading_enabled: false`. Restart after changing the
configuration. The dashboard reports the effective mode without changing it.

The default API consumer group is the shared `kafka.group_id` plus
`.api.account-<account_num_id>`. Each account must see every command before
filtering. An explicit `api.kafka_group_id` override must therefore be unique
per account. Separate accounts also need separate session and journal files.

## Commands

| Command | Network effect | Result |
| --- | --- | --- |
| `status` | None | Local setup checks, not proof of server login. |
| `import-session` | None | Import/signature result; no credentials printed. |
| `inspect-emulator` | ADB reads only | Selected AVD/serial, root access and saved-login availability. |
| `import-emulator-session` | ADB reads only | Import saved session from the supported emulator build. |
| `capture-settings` | None | Resolved non-secret machine/capture settings. |
| `listen` | Kafka and signed HTTP requests | Account publishing and validated command execution when enabled. |
| `check-session` | Two signed reads | Ordinary login validity and populated trading-auth state. |
| `holdings` | One signed read | Raw response containing `data.holdList`. |
| `cash` | One signed read | Raw currency/account rows. |
| `orders` | One signed read | Today's order records, including rejected orders. |
| `plan-order` | None | Unsigned Buy/Sell Market, Limit or timed-cancel plan. |
| `plan-cancel` | None | Unsigned plan using `orderTxnReference`. |

Successful commands exit 0. Setup, capture, authentication, broker and usage
errors exit 2. Query JSON contains private account data; keep redirected output
local. Error messages omit raw broker bodies and credentials.

Requests use the observed HTTPS host, fresh timestamps and exact decimal signing.
The transport restricts account, quote and typed order endpoints, disables
environment proxies and redirects, and makes no automatic broker-write retry.
The listener's separate recovery path can send a password-login request using
the imported device identity. It does not call the captured token-refresh
endpoint. Normal API calls and routine password re-login require no capture
library or Android process after the initial import.

## Start, stop and publish

```powershell
.\start_zhuorui_listener.ps1
.\check_zhuorui_listener.ps1
.\start_zhuorui_monitor.ps1 -OpenBrowser
.\stop_zhuorui_listener.ps1
```

Start/check/stop now default to the API backend. The Control Room manages that
listener and reports publication status and trading mode. Scheduled emulator
restarts are disabled in API mode; listener restart preserves the emulator login.
The stop launcher requests graceful completion of active work. It does not
force-kill an API process that is still finishing its command.

For foreground operation, run:

```powershell
.\.venv-api\Scripts\python.exe .\zhuorui_api.py listen
```

Account snapshots use the existing KTrader `account-details` format, mapped from
API account, cash and holdings responses. They publish at startup and every 30
seconds. Each order submission attempt queues an immediate extra publication,
including rejected or unknown submissions; cancellation attempts also queue one.
The independent worker prevents holdings reads from delaying the FOK timer. If
a publication is already in flight, the next request waits in its queue. Broker
or Kafka outages can delay or fail delivery; local state and logs report this.

API command results always publish to the configured `order-status` topic,
including disabled, rejected, duplicate and unknown results. The legacy
`kafka.publish_order_status` switch does not disable API results. Broker
acknowledgement does not prove a fill or successful cancellation.

## Kafka command contract

Each JSON command needs an explicit matching `account_id` or `account_num_id`.
Every provided account or server selector must match. Other-account messages
are ignored. Producer `id` / `command_id` is preferred; otherwise the listener
uses Kafka topic/partition/offset as a stable identity.

Examples of accepted shapes; replace IDs and order fields before use:

```json
{"id":"example-market","account_num_id":1,"type":"MARKET_ORDER","symbol":"BILI","side":"buy","qty_shares":1}
{"id":"example-limit","account_num_id":1,"type":"LIMIT_ORDER","symbol":"BILI","side":"sell","qty_shares":1,"limit_price":"25.1000"}
{"id":"example-fok","account_num_id":1,"type":"LIMIT_ORDER_FOK","symbol":"BILI","side":"buy","qty_shares":1,"limit_price":"25.1000"}
{"id":"example-cancel","account_num_id":1,"type":"CANCEL_ORDER","orderTxnReference":"EXAMPLE_BROKER_REFERENCE"}
```

Buy and Sell Market commands send native `entrustProp: MO`, omitting
`entrustPrice`, `allowPrePost` and native FOK flags. A Market command containing
a price is rejected. Limit sends `LO` and rounds its price to cents before signing:
Buy rounds up and Sell rounds down (for example, `322.1064` becomes `322.11` for
Buy or `322.10` for Sell). Exact-cent prices retain their value. A Sell price that
rounds to zero is rejected locally. This also applies to timed-cancel Limit orders.
The journal keeps the original requested price for command identity and auditing.

Limit and timed-cancel Limit orders default to `allowPrePost: "Y"`, the app's
captured instruction permitting regular and pre/post trading. An explicit
`allow_pre_post: false` (or equivalent alias) sends `"N"` for regular hours only.
The offline CLI follows the same defaults; `--no-allow-pre-post` selects regular
hours only. `sessionType` is an order-response field, not a captured submission
parameter. Native Market orders retain the app's `MO` request without a price
or extended-hours override. Extended-hours native Market submission is not
verified or implemented. A Market order submitted before regular hours may remain pending with the
broker until the market opens. Check its broker status before submitting again.
Shares must be positive integers.

Market also accepts `notional_usd` without shares. The client requests a fresh,
real-time quote for the exact US symbol and rounds down to whole shares. Delayed,
stale, ambiguous or inactive quotes reject this sizing mode; use `qty_shares`
when a suitable quote is unavailable. Quote-based sizing is not a guaranteed
maximum execution cost: the final order remains Market. If both shares and a
notional amount are supplied, explicit shares take precedence.

`LIMIT_ORDER_FOK`, `fok`, `fill_or_kill`, and Limit with `time_in_force: FOK` all
use the one-second cancellation policy. It submits `LO` and targets cancellation
one second from dispatch. Acknowledgement latency counts toward the second; a
late acknowledgement triggers cancellation when the reference is known.
Network delays can miss the target, and full or partial fills remain possible.
This is not native all-or-none FOK. An unknown submission without a reference
is never guessed or blindly resubmitted.

Cancellation sends only the confirmed `orderTxnReference`, timestamp and
signature. A specific reference must be uniquely present in this account's US
orders today. Without a reference, legacy cancel commands mean cancel-all for
eligible US orders. Validated status values determine eligibility; terminal and
pending-cancel orders are skipped, and unknown statuses prevent broad cancellation.
The script does not infer eligibility from `isOpen`, which was true even for
captured rejected orders. Producer `order_id` is a command-ID alias, not a broker
reference.

## Duplicate protection and recovery

Kafka timestamps are required. Commands older than 120 seconds by default,
unexpectedly future-dated commands, and expired payloads are rejected before
execution. SQLite records each command before dispatch; Kafka offsets are
committed after processing. Redelivery never submits the same command again,
and reusing one ID for different order details is rejected. An operating-system
lock prevents simultaneous listeners from using one journal.

Timeouts, unreadable responses and interrupted submissions are marked unknown.
New execution pauses until the account's actual order state is reconciled and
the journal is deliberately resolved. Do not erase the journal to force a retry.
Interrupted cancellation requests are not blindly repeated. Known timed-cancel
orders whose cancellation was not yet dispatched can be recovered on restart.

## Login recovery

The listener distinguishes invalid-session code `000102` and login-elsewhere code
`000112`. Both pause new account operations and schedule automatic same-device
password login when `api.auto_login_enabled` is true (the default). The mappings
come from the app; failure cases have synthetic tests. Timeouts, generic HTTP
failures and ordinary order rejection are not themselves logout signals.

The first confirmed logout detection fixes the recovery deadline:

| Beijing time at detection | First login attempt |
| --- | --- |
| 09:00 inclusive to 16:00 exclusive | Five minutes after detection. |
| All other times | Immediately eligible at the next command boundary. |

For example, detection at 15:59 waits until 16:04. Repeated errors do not extend
the delay. The schedule is persisted next to the encrypted cache as
`session.recovery.json` when the cache is named `session.dpapi`; restarting the
listener preserves the original deadline and any blocked state. It contains
recovery metadata, not credentials or session tokens.

Password login reuses the imported broker user/device identity and the shared
login credentials. The request reproduces the captured MD5 password field and
signed app flow. The returned phone/user identity must match before the new
session replaces the DPAPI cache. No new emulator or device identity is created.
Recovery runs on the command thread between commands, never inside submission
or cancellation handling, so it cannot interrupt a timed order's cancellation
sequence. After login, the listener requests a fresh holdings publication.

During recovery, incoming trades are rejected with their status recorded;
login success does not replay them. Existing unknown submission/cancellation
outcomes remain unresolved and must be checked separately. Send a fresh command
only when appropriate; do not reuse an old ID to force replay.

Phone-verification code `010007`, other explicit login rejections, missing or
invalid credentials, and unexpected login identities pause automatic attempts
for review. Temporary transport failures schedule another attempt after
`api.login_retry_seconds` (default 300 seconds). Set `api.auto_login_enabled`
to false to disable password login recovery. Zhuorui's one-login rule still
applies: a successful recovery may invalidate a login opened elsewhere.

For manual recovery, restore the same emulator's login, run
`import-emulator-session` when root reads are available, then `check-session`.
Alternatively refresh an account page with capture active and run
`import-session`. The running listener notices the replacement encrypted session.
Resolve credentials or verification in the app before importing that valid login.

The existing DPAPI cache is preferred at startup. Optional
`api.auto_import_session: true` allows local emulator import when needed; it
does not overwrite a usable cache from a logged-out emulator. Import itself
performs no password login, root enabling, emulator restart or broker-write
retry. Its default is false. Leave it false for routine operation with the
emulator stopped; direct password recovery does not depend on it.

The listener status JSON is a diagnostic snapshot. If Windows temporarily blocks
replacement, or another filesystem error prevents saving it, the listener keeps
the previous complete file and retains current values in memory. It logs one
warning per outage and retries the latest values on subsequent updates, including
the 15-second heartbeat, without sleeping in order processing. Recovery is logged.
The monitor's existing overdue-status indicator flags a prolonged outage.
This handling applies only to the status snapshot; order-journal and encrypted
session/recovery persistence failures retain their existing error handling.

Runtime diagnostics are written immediately to the current listener's
`logs/zhuorui_api_listener_*.out.log`; its exact path is recorded in
`zhuorui_api_listener.current.json`. Each diagnostic includes a UTC timestamp,
severity, process ID and thread. Logs cover startup and effective trading mode,
publication starts and acknowledged deliveries, login recovery, command receipt
and Kafka offset commits, and shutdown reasons and cleanup failures. A heartbeat
every minute records uptime, session status and the latest confirmed publication;
the status-file heartbeat remains every 15 seconds. Failures identify the stage
(for example `cash_query`, `kafka_ack` or `kafka_commit`), exception types and code
locations. Credentials, raw broker responses, account contents and exception
messages are omitted. Logging failures do not interrupt account operations.

Control Room writes its lifecycle, control actions, listener state changes and
minute heartbeats to `logs/zhuorui_monitor_*.out.log`. Diagnostic errors also go
to these stdout logs; check the corresponding stderr logs for other runtime
output. A forced termination or power loss cannot write its own shutdown reason.
On the next API start, an unfinished prior status snapshot produces a warning
with the previous PID and last update time; it does not assert a cause. These
logs add evidence, not automatic restart behavior.

Trading-password authorization is separate from ordinary login. Before an order
or cancellation, the listener checks account and trading authorization. If locked,
it uses the existing `trade_password` setting (also accepting the UI aliases
`trade.password` and `password`) to call `/as_trade/api/account/v1/auth`.
The request uses the account response's `clientId`, a fresh timestamp/signature,
and randomized SM2/SM3 encryption matching Android 3.1.5: lowercase hex
C1 || C3 || C2 without the initial 04 point byte. Install the updated
`requirements-api.txt` for the pinned `gmalg` dependency.

The listener verifies the unlock response's user and re-queries trading
authorization, requiring both user and account to match before dispatch. A failed
or uncertain unlock rejects the command and pauses further password attempts for
that listener instance. Correct the password or account verification before
restarting; a verified unlocked response also clears the pause. Ordinary login
recovery does not clear the pause. No rejected order is automatically replayed.
Already-unlocked sessions and holdings queries do not submit the trading password.
No full account re-login occurs merely because trading is locked.

Offline encryption vectors, request signing, identity checks, failed-attempt
handling, and order/cancellation integration are tested. This implementation has
not yet been validated with a live trading-password request.

Captured Market/Limit submissions and cancellation requests establish wire
formats. The observed orders were rejected outside the trading session with
zero fills. Buy/Sell mapping is supported by the APK enum. Successful execution,
successful cancellation still need controlled validation. Direct recovery from
`000112` was verified on 2026-09-10: logout was detected at 11:24:24 Beijing,
password login succeeded at 11:29:25, and holdings publication resumed immediately.
Two subsequent periodic publications were acknowledged about 30 seconds apart.
See [login recovery evidence](../api_research/login-recovery-evidence.json).
Other expiry and verification branches remain covered by offline tests.

## Finish capture

When the manual capture session is finished:

```powershell
.\api_research\finish_original_capture.ps1 -Visible
```

This restores the proxy and normal emulator boot, retains current app data and
does not restart the UI listener. The private captures, APK, CA keys and emulator
backups remain in `api_research/private`. Do not move that folder while its proxy
or emulator is running.
