# Emulator-free Zhuorui feasibility investigation

Investigated on 2026-09-08 against the locally installed Android app
`com.zhuorui.securities`, version **3.1.5** (315001).

**Conclusion: an emulator-free client looks technically feasible. HTTPS capture,
independent request signing, and direct authenticated holdings/cash reads are
proven; order execution is not yet proven.** This folder contains investigation tools, not a
replacement trading backend.

The application code now lives under `zhuorui/`. A shared-config API CLI, Kafka
trading listener and offline order planner are available through `zhuorui_api.py`; see
[the project run instructions](../README.md) and [API guide](../docs/api.md).
The runtime imports sessions into encrypted local storage and implements order
transport, account publishing and automatic same-device password login recovery.
The code defaults to enabled execution; research validation used an explicit
disabled override and submitted no orders. Implemented flows
and offline tests do not establish successful live fills or cancellations.
This research folder retains active capture paths, private artifacts and protocol
evidence; runtime and research share `zhuorui/api/signing.py`.

Latest capture, 2026-09-10: recording was restored on the same emulator after a
normal restart. The user signed in, unlocked trading, and selected Market on
the BILI Buy ticket without submitting. Login, unlock, and preview returned
HTTP 200 / `000000`. The Market preview used `entrustProp="MO"` with no
`entrustPrice`; the preceding Limit preview used `"LO"` with a price. All four
request signatures were reproduced offline. The MD5 password-login field has
since been reproduced and used in the direct recovery implementation. Direct
recovery from `000112` succeeded on September 10 after the configured five-minute
Beijing-time delay, with immediate holdings publication and subsequent 30-second
periodic publications. See [login recovery evidence](login-recovery-evidence.json)
and [market-preview evidence](market-preview-evidence.json). Other expiry branches
and long-running renewal still need further observation.

The user then submitted one Market order and two Limit orders, and revoked the
latest Limit order. The Market submission used `MO` with neither `entrustPrice`
nor `allowPrePost`. Limit submissions used `LO`, a decimal price, and explicit
`allowPrePost` (`Y` in one case, `N` in the other). All three submissions returned
order references and `000000`; subsequent records for the first two showed
`NOT_TRADE_SESSION`, rejected status `9`, and zero fills. Cancellation sent only
`orderTxnReference`, `timeStamp`, and `sign` to `entrust_withdraw`; its reference
exactly matched the latest Limit acknowledgement. It returned `000000` without
a data object. The user's subsequent Today's Orders refresh matched both order
references and showed the latest Limit order rejected with `NOT_TRADE_SESSION`
and zero fills. A successful cancellation is therefore not established.
All four signatures were reproduced offline, without replaying requests. See
[market-limit-cancel-capture-evidence.json](market-limit-cancel-capture-evidence.json).
Request formats are now captured; successful execution and a confirmed
cancellation remain unverified.

Follow-up: [43 offline order-flow, holdings, and signing tests](SIMULATION_RESULTS.md)
now cover market, limit, native FOK, the existing timed-cancellation behavior,
holdings changes, read-only API guards, and the requested one-second cancellation
strategy. Native FOK remains a simulator comparison, not the requested API flow.
Authenticated holdings and cash reads are now verified, as described below;
successful execution and confirmed cancellation remain unverified.

Initial authenticated capture (2026-09-09): **readable HTTPS capture works on the
existing logged-in `Pixel_10_2` / `emulator-5554`.** The user authorized restarting
this same emulator; a new-device login was unavailable because it would require
phone verification. A temporary debugging ramdisk was first tested on an empty
AVD using the exact Google Play system image. The original AVD then received a
verified 25.2 GiB backup and was restarted with that temporary ramdisk. Its
Android ID, system fingerprint, app data, and installed system images were
retained. SELinux remains enforcing and ADB authentication remains enabled.

The original app successfully refreshed its existing session and returned
account information through the capture proxy, without a new login or phone
verification. This verifies session retention and readable account traffic.
The user's subsequent holdings refresh also enabled successful direct Windows
API reads of holdings (**0.297 s**) and cash (**0.844 s**), each returning HTTP
200 / `000000` with a fresh signature. Position identities, total and available
quantities, costs, and the selected cash fields matched the captured app
responses. Price-dependent valuation, margin, and buying-power fields changed
between observations. These are individual timings, not performance guarantees.
The direct probes use no ADB calls and explicitly disable proxy use; they still
use the captured app session. The later runtime implements direct password
re-login, while those original probes did not validate renewal or recovery.
See [account-read-evidence.json](account-read-evidence.json).
Successful execution and confirmed cancellation remain pending. The September 10
capture above includes native Market submission and a cancellation request. See
[original-capture-evidence.json](original-capture-evidence.json) for non-secret
evidence and [the capture checklist](CAPTURE_CHECKLIST.md) for the next user step.
The trading listener was stopped for that manual capture session; this historical
note does not describe the current runtime process state.

Requirement clarified on 2026-09-09: **API market orders must be native market
orders.** Do not carry the UI's price-adjusted limit substitution into the API
backend. See [the capture checklist](CAPTURE_CHECKLIST.md) for the remaining
user-assisted validation.

## Verified results

- The user's BILI Limit submission and three trading-password attempts were
  captured. Two incorrect passwords returned application code `350117`; the
  third attempt returned `000000` and populated the trading-auth state.
- The Limit request used `entrustProp="LO"`, `ts="US"`, `entrustBs="1"`,
  `allowPrePost="N"`, `apStatus=0`, and `volumeMultiple=1`. It included price
  and quantity, but omitted `timeInForce`; the later order record showed `DAY`.
  These are observations of this case, not assumed defaults for every order.
- Submission returned `000000` with string `orderNo` and `orderTxnReference`.
  Four later status observations matched both references and showed
  `entrustStatus="9"`, zero `businessAmount`, and `ORDER_EXECUTE_FAILED`.
  Submission acknowledgement therefore does not establish an open or filled
  order. `isOpen` was true even in this rejected record and must not be treated
  as proof of cancellation eligibility. The user reported a trading-hours
  rejection; the captured HTTP fields contain only a generic execution failure.
- All three auth signatures and the Limit signature were reproduced offline.
  The latter required preserving the decimal price's trailing zeroes. Signing
  now supports exact `Decimal` values, with five added regression tests. The
  encrypted trading-password field was observed. Its SM2 transformation has
  since been derived, but automatic trading unlock remains unimplemented.
  No auth or order request was replayed for this capture evidence.
  See [limit-auth-capture-evidence.json](limit-auth-capture-evidence.json).
- A 140-second TLS-passthrough proxy capture of the original emulator observed
  `backendpro.zr.hk:443`. The existing listener completed holdings queries during
  the capture. This pass recorded destinations only, without decrypting TLS.
- The original Android 16 Google Play image refused `adb root`. A separate
  `ZhuoruiCapture` emulator was created from the already installed Android 36.1
  Google APIs image, using private project data and serial `emulator-5580`.
- A temporary system CA mount in that test emulator enabled HTTPS decryption.
  The original APK required no modification or certificate-pinning bypass for
  the **observed public HTTP calls**. This does not establish that every app
  feature uses the same TLS configuration.
- The proxy captured **21 HTTP responses across 17 paths** from the logged-out
  test app. Request bodies are JSON; common fields include `timeStamp` and `sign`.
- The app sorts the unsigned JSON fields, adds a millisecond timestamp, and
  generates a Base64 RSA PKCS#1 v1.5 signature using SHA-1. The signing material
  is available in the installed APK's configuration assets. A Python probe
  reproduced a captured signature exactly without exposing the key.
- With the **test emulator and proxy stopped**, Python sent a **freshly signed**
  request directly to
  `https://backendpro.zr.hk/as_common/api/app_version/v1/get_new_version`.
  The result was HTTP **200**, application code **`000000`**, in **0.234 seconds**.
  This was an unauthenticated version query; its timing is not a holdings
  benchmark. The original trading emulator remained running, but the probe
  contains no ADB calls and used no proxy or Android process.

Machine-readable, non-secret results are in [evidence.json](evidence.json).
The APK SHA-256 is recorded there to bind these findings to the inspected build.

## Private API candidates found in the app

These are static APK findings, **not authenticated endpoints verified by this
investigation**. Their current request requirements and business behavior need
to be confirmed through account-session capture.

| Purpose | Candidate path |
| --- | --- |
| Holdings | `/as_trade/api/order/v1/get_hold_list` |
| Alternative client asset holdings | `/as_trade/api/client_asset/v1/hold_list` |
| Cash/funds | `/as_trade/api/funds/v1/info` |
| Account asset details | `/as_trade/api/client_asset/v1/client_asset_detail` |
| Today's orders | `/as_trade/api/order/v1/get_today_entrust` |
| Order entry | `/as_trade/api/order/v1/entrust_enter` |
| Order amendment | `/as_trade/api/order/v1/entrust_modify` |
| Order cancellation | `/as_trade/api/order/v1/entrust_withdraw` |
| Trading authentication | `/as_trade/api/account/v1/auth` |
| Trading-password check | `/as_trade/api/auth/v1/check_trade_pwd` |
| Password login | `/as_user/api/user_account/v1/user_login_pwd` |

Relevant request/response classes also remain readable in the APK:

- `MyHoldListRequest`: `market`.
- `MyHoldListResponse.HoldListItem`: `code`, `ts`, `market`, `currentAmount`,
  `enableAmount`, `costPrice`, `last`, and other valuation fields.
- `OrderActionAddRequest`: `code`, `ts`, `entrustBs`, `entrustAmount`,
  `entrustPrice`, `entrustProp`, `timeInForce`, `allowPrePost`, and additional
  optional order fields. Their enum values were not assumed or tested.
- `OrderActionAddResponse`: `orderTxnReference` and `riskTypeMsg`.
- `OrderActionCancelRequest`: `orderId`, `orderTxnReference`, and `remark`.
- `TradeAuthRequest`: `clientId` and `password`. The SM2 transformation has been
  derived; API trading-password submission and unlock remain unimplemented.

Static header logic includes `token`, `userId`, `deviceId`, device metadata,
`appVersion`, OS version, and language. The logged-out capture directly confirmed
the device/app/language headers; it did not contain account authentication.
The app separately handles expired login and trade authorization states.

## Implementation path for this repository

The following was the investigation roadmap. The runtime now implements the
account mapping, Kafka listener, native Market/Limit transport, timed cancellation,
journal, dashboard controls and same-device password recovery. Remaining live
validation limits are described above and in [session handling](SESSION_HANDLING.md).

1. Capture an authenticated holdings refresh, funds refresh, and order-history
   query in a controlled account session. Determine login/device verification,
   token expiry and renewal, trading unlock, and any password transformation.
   A new-device login may invalidate the existing trading session, so choose an
   appropriate account and operating window before this step.
2. Build a read-only API client and compare its cash and holdings with the app.
   Validate currencies, market identifiers, pagination, empty positions, and
   total versus available quantity. Establish whether the cash value is ledger
   cash, settled cash, or buying power before mapping it.
3. Introduce a backend interface at the account/trade operation level. Preserve
   Kafka command normalization, account selection, and the published snapshot
   format. Have the API backend return the existing `cash` and `securities`
   structure used by `ktrader_account_snapshot`; avoid UI navigation calls in
   the API path. Keep the UI backend available during migration.
4. Map order types and cancellation semantics only after controlled validation.
   The API backend must use native market orders, as explicitly requested on
   2026-09-09; do not use a quote-derived limit as a substitute. If native market
   is unavailable for an instrument/session, return an explicit unsupported
   result. The user's API replacement for the existing FOK command is a limit
   order with cancellation targeted one second after submission (clarified on
   2026-09-09). It permits partial fills and is not native FOK. Count the deadline
   from request dispatch; if order identity is not yet known, reconcile before
   cancelling and never blindly resubmit.
5. Add durable command deduplication and order reconciliation before enabling
   order submission. A timeout after sending an order is an unknown outcome;
   query broker order state before retrying. Do not port the current UI retry
   loop directly to order-entry HTTP calls. Obtain broker order references and
   distinguish acceptance, rejection, partial fill, fill, and cancellation.
6. Make monitoring backend-aware: an API worker needs session health and request
   timings, and should not depend on emulator health or scheduled emulator
   restarts. Change the live configuration only after account-level validation.

The transport/signing and account-read proofs reduce uncertainty substantially.
The user clarified on 2026-09-10 that an authenticated emulator will be available,
with new-device phone verification outside scope. Initial import establishes the
account and device identity; routine API password recovery now reuses that identity
without needing the emulator to run. Login detection from 09:00 inclusive to
16:00 exclusive Beijing time waits five minutes from the first detection; outside
that window recovery is immediately eligible between commands. The deadline
survives restart. Explicit login/verification errors pause attempts, transport
failures retry after the configured interval, and recovery never replays an order.
See [SESSION_HANDLING.md](SESSION_HANDLING.md) for implementation and evidence limits.
The research tools sent one direct
holdings query and one direct cash query after the user's refresh. No account
login, order entry, amendment, or cancellation was performed by those tools.

## Tools and private artifacts

| File | Purpose |
| --- | --- |
| `capture_destinations.py` | Bounded metadata capture on the configured emulator; rejects pre-existing proxies and restores all proxy settings. |
| `capture_metadata.py` | Destination-only mitmproxy addon. |
| `start_capture_emulator.ps1` | Starts the separate headless test emulator on port 5580. |
| `prepare_capture_session.ps1` | Starts the isolated emulator, proxy, and CA mount together; `-Visible` shows the app for a manual capture session. No automated login or orders. |
| `start_api_proxy.ps1` | Starts a localhost-only proxy on port 8082, restricted to the observed backend host. |
| `trust_capture_ca.py` | Mounts the temporary CA on an explicitly verified capture AVD; original-device use additionally requires the verified backup record and original fingerprint. |
| `prepare_debug_ramdisk.py`, `start_boot_test.ps1` | Build and test a temporary Android debugging boot without modifying installed SDK images. |
| `backup_original_emulator.py`, `start_original_debug_session.ps1` | Preserve and verify the existing AVD before its authorized temporary capture boot. |
| `finish_original_capture.ps1` | Restore the original proxy and cold-start the same AVD normally, retaining current app data and leaving the listener stopped. |
| `capture_api.py` | Stores broker HTTP flows plus a schema-only index. |
| `inspect_apk.py` | Extracts endpoint and network bytecode evidence locally. |
| `probe_public_api.py` | Offline signature verification; `--send` permits exactly one public version query. Refuses captured account-auth headers and arbitrary paths. |
| `probe_holdings_api.py` | Offline account-read verification; optional single read-only request requires a fresh successful authenticated capture. |
| `simulated_broker.py` | Offline synthetic order/holdings model; no network or UI access. |
| `test_simulated_flows.py`, `test_readonly_probe.py` | 29 offline simulation, reconciliation, signing, and account-query guard tests. |
| `simulated_timed_cancel.py`, `test_timed_cancel.py` | Offline one-second limit-order cancellation strategy and 9 additional timing/identity tests. |
| `test_signing_decimal.py` | Five offline regressions for exact decimal signing, including price scale and invalid numbers. |
| `stop_capture.ps1` | Stops only the verified temporary emulator/proxy. |

Dependencies are isolated in `api_research/.venv/`; the production environment
and requirements were not changed. The exact research dependency versions are
in this folder's `requirements.txt`.

`private/` contains the copied APK, original flow captures, proxy CA/private key,
temporary AVD data, logs, extracted bytecode, and local probe result. It is
excluded from Git, as is the research environment. **Raw captures and APK key
material must remain local; the schema index removes values but raw flows do
not.** Do not share the private directory or install this CA on the Windows
host. Original-emulator use is a temporary mount in the explicitly authorized,
backed-up capture session; normal restart removes it.

For offline verification from the project root:

```powershell
.\api_research\.venv\Scripts\python.exe .\api_research\probe_public_api.py
```

Adding `--send` performs the bounded public query against Zhuorui. It does not
start an emulator or proxy and cannot place or cancel orders.

To resume isolated capture, start `start_capture_emulator.ps1`, wait for
`emulator-5580` to finish booting, enable its supported `adb root`, start
`start_api_proxy.ps1`, and run `trust_capture_ca.py`. Then set **only the test
emulator's** proxy to `10.0.2.2:8082` and launch the copied app. This procedure
does not authenticate the test app. Finish with `stop_capture.ps1`.

## Cleanup and validation

For the current original-emulator capture, run `finish_original_capture.ps1`
when the manual session is finished. This restores its saved proxy settings and
starts the same AVD with its normal boot image, using current app data. It does
not restore the old backup over newer app state or restart the listener. The
new normal-boot cleanup helper is syntax checked but has not yet been run; the
manual capture is still active. `stop_capture.ps1` alone restores connectivity
and stops the proxy, but a normal cold start is still needed to remove the
temporary debugging mode and certificate mounts.

Historical cleanup from the initial separate-device investigation:

The separate emulator and HTTPS proxy were stopped after capture. The original
emulator's active and persisted proxy state was cleared and verified. Android
retains derived `global_http_proxy_*` fields if only `http_proxy` is deleted;
cleanup must apply `:0` first, verify the active proxy is cleared, and restore
the saved fields. The metadata-capture helper was updated accordingly.

Validation included a real TLS capture, exact offline signature reproduction,
and a successful direct public API request. Research Python files were syntax
checked and PowerShell files parsed. The revised reusable proxy cleanup routine
was not exercised in another full production capture; its no-proxy cleanup
sequence was verified directly on the original emulator. Existing changes in
`zhuorui_market_order.py` and its tests predated this investigation and were left
untouched.

Primary technical references: [mitmproxy Android CA guidance](https://docs.mitmproxy.org/stable/howto/install-system-trusted-ca-android/),
[mitmproxy capture modes](https://docs.mitmproxy.org/stable/concepts/modes/), and
[Android's ProxyTracker implementation](https://android.googlesource.com/platform/packages/modules/Connectivity/+/refs/heads/main/service/src/com/android/server/connectivity/ProxyTracker.java).
The original-device debugging boot follows Android's
[debug ramdisk mechanism](https://source.android.com/docs/core/tests/vts/vts-on-gsi).
The API and signing findings above come from the local APK and captured traffic,
not public API documentation.

## Portable setup and direct session import

Active capture launchers now share the `capture` section in the main configuration.
Use `-ConfigPath` for PowerShell helpers or `--config` for Python setup helpers.
See [Windows portability](../docs/windows-porting.md) for each setting and the
original-emulator prerequisites. Pass `-Visible` to original-emulator start/finish
helpers for manual app operations; their default is a hidden emulator.

`zhuorui_api.py import-emulator-session` can read the saved login directly from
the validated, root-readable original emulator. It does not require an initial
traffic capture on another account using that build. See [verification evidence](emulator-session-evidence.json)
and [the API guide](../docs/api.md). Automatic same-device password login and live
order transport are implemented. Execution defaults to enabled, with an explicit
disabled override available; automatic trading-password unlock remains pending.
The storage decoder follows the public [MMKV map format](https://github.com/Tencent/MMKV/blob/master/Core/MiniPBCoder.cpp)
and [metadata layout](https://github.com/Tencent/MMKV/blob/master/Core/MMKVMetaInfo.hpp),
with stricter checksum/format rejection for session credentials.
