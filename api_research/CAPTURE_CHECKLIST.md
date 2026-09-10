# Captures needed for the emulator-free client

Portability update: active capture helpers read the main config's `capture`
section and accept `-ConfigPath` (PowerShell) or `--config` (Python). Historical
device names below describe this investigation; new machines use their configured
original emulator. See [the porting guide](../docs/windows-porting.md). Use
`-Visible` with original-emulator start/finish helpers for manual app operations.
Direct session import is now available for the supported root-readable app build;
recording traffic is still needed to validate unproven order flows.

Session update, 2026-09-10: an intervening normal emulator restart removed the
temporary certificate/debugging mode while leaving the proxy setting present.
Capture was restored after verifying device identity and making a fresh backup;
new decrypted public requests succeeded. The user then signed in on this same
emulator, unlocked trading, and opened the BILI Buy ticket with Market selected.
Password login, trading unlock, and the Market preview all returned HTTP 200 /
`000000`. The preview used `entrustProp="MO"` and omitted `entrustPrice`; the
preceding Limit preview used `"LO"` with a price. Both preview signatures and
both authentication request signatures were reproduced offline. See
[market-preview-evidence.json](market-preview-evidence.json) for this initial step.
The user subsequently submitted a Market order and two Limit orders, then
revoked the latest Limit order. All requests and acknowledgements were captured;
the first two orders were reconciled as rejected with `NOT_TRADE_SESSION` and
zero fills. The cancellation used only the latest acknowledgement's
`orderTxnReference`, plus timestamp and signature, and returned `000000` with no
data object. The subsequent Today's Orders refresh succeeded and matched both
references for the latest Limit order: rejected status `9`, `NOT_TRADE_SESSION`,
and zero fills. This confirms the final observed rejection, not a successful
cancellation. The requested refresh is complete. See
[market-limit-cancel-capture-evidence.json](market-limit-cancel-capture-evidence.json).
Yesterday's captures and successful account-read
evidence remain valid historical results. Do not treat a saved `recording`
status as proof of current readiness after a restart: check the live certificate,
proxy, listener, and newly captured responses. The original app was not reinstalled
or restored from yesterday's backup.

Updated 2026-09-09. The API client's market orders must be **native market
orders**. The earlier price-adjusted limit approach is a UI simplification and
will not be used by the API backend. This corrects the migration requirement;
the running UI script is unchanged.

The user also specified that the API replacement for the existing FOK command
should be a limit order with cancellation targeted **one second after
submission**. It is a timed-cancellation strategy and permits partial fills.
The timer starts when the request is sent. Late acknowledgement can delay the
actual cancellation; missing order identity requires reconciliation first.

The installed APK separately defines `OrderType.MARKET`, `OrderType.LIMIT`, and
`OrderTimeInForce.FOK`. These are static app findings, not proof that a particular
account, instrument, or session accepts each setting. Enum ordinal positions
are not assumed to be wire values. Relevant bytecode remains in the ignored
`private/order-type-bytecode.txt` file.

## Start with authentication and holdings

**Current constraint (2026-09-09): use the existing logged-in `emulator-5554`
(`Pixel_10_2`).** The user cannot log in on the separate emulator because that
requires phone verification. Do not use the separate-device login procedure
below for this session. Do not sign out, clear app data, reinstall the app, or
change the original emulator's system image as an incidental capture step.

The original listener was verified stopped. The original Android image is a
`user` build with `ro.debuggable=0`; the installed app targets API 35, is not
debuggable, and has no manifest network-security override. The inspected legacy
HTTP client uses the default certificate-chain trust manager (although it
disables hostname verification). Recent app logs did not contain HTTP request
or response evidence. These findings do not establish an in-place decryption
method, nor prove that all endpoints use the legacy client. A TLS-passthrough
proxy records destinations only and is insufficient for order-flow discovery.

The user subsequently authorized restarting the same emulator. Android's
temporary debug ramdisk mechanism was validated on an empty AVD with the exact
Google Play system image, including the certificate mount, enforcing SELinux,
and authenticated ADB. After making and verifying a complete 25.2 GiB backup,
the original AVD was restarted with that temporary boot file. Its Android ID
and system fingerprint matched their saved values. Its installed system image
and app data were retained; no app reinstall or new-device login was performed.

**Capture is now active on the original emulator.** The existing app's
`refresh_token`, `account/info`, and `current_auth_info` responses were captured
with HTTP 200 and application code `000000`. The listener remains stopped.
Account login retention is therefore observed. The user then refreshed holdings,
cash, and orders. Both direct account probes returned HTTP 200 / `000000` using
fresh signatures and the existing session. Position identities, quantities,
available quantities, costs, and selected cash fields matched the app captures;
valuation, margin, and buying-power fields changed between observations.
The observed holdings body is `data.holdList`; cash is a list in `data` keyed by
account and `moneyType`. Both captured requests contained only `timeStamp` and
`sign` in the body. Do not assume the static optional `market` request field is
required, or equate `cashAmt`, `enableBalance`, and `fetchBalance`.

Today's orders and historical orders were also captured successfully. Today's
orders had no populated `data` in this example; an empty result must be handled.
Independent token renewal remains unverified.

The user next submitted a BILI Limit order and intentionally supplied two wrong
trading passwords before the correct one. `/account/v1/auth` returned `350117`
twice and `000000` once. All three request signatures were reproduced offline;
no authentication attempt was sent by the research tools. Further intentional
password failures are unnecessary for this case. Password encryption remains
to be implemented independently.

The Limit body used `entrustProp="LO"`, `ts="US"`, `entrustBs="1"`,
`allowPrePost="N"`, `apStatus=0`, and `volumeMultiple=1`, with numeric price and
quantity. It omitted `timeInForce`; order history showed `DAY`. Its signature
was reproduced after preserving decimal scale rather than converting prices
to binary floats. Submission returned `000000` and two string order references;
later records matched both references and showed rejection (`entrustStatus="9"`),
zero fills, and `ORDER_EXECUTE_FAILED`. The user reported an hours restriction;
the captured HTTP reason is generic. Notably, `isOpen` was still true in this
rejected record, so it cannot establish that an order can be cancelled.

Native **Market** selection, submission, and a cancellation request were captured
on 2026-09-10. The Market submission used `entrustProp="MO"`, omitted price and
`allowPrePost`, and used `entrustBs="1"` for the user's reported Buy action. The
two Limit submissions included a price and explicit `allowPrePost` (`Y` then
`N`). None explicitly included `timeInForce`. Cancellation supplied only
`orderTxnReference`, `timeStamp`, and `sign`; do not require the static class's
optional `orderId` or `remark` fields for this observed flow. All four request
signatures were reproduced offline. An acknowledgement with `000000` does not
establish successful execution or a cancelled state. Today's Orders was refreshed
successfully after cancellation; the matched latest Limit record showed rejection
and zero fills. No more order
submissions are needed for this capture step. No manually timed one-second cancellation
is required; that timer belongs in the offline-tested client strategy.

Use `finish_original_capture.ps1` after
the session to restore the original proxy and normal boot. Backups are recovery
artifacts; normal cleanup preserves current app data instead of rolling back to
the earlier backup.

### Separate-device procedure (not usable for the current session)

1. Choose a time when the production trading listener can be paused for an
   account capture session. A manual login on the separate device may invalidate
   the current trading session. The setup helper does not pause or resume the
   production listener automatically.
2. Start the separate capture environment from the project folder:

   ```powershell
   .\api_research\prepare_capture_session.ps1 -Visible
   ```

   This opens the existing test app with the local HTTPS proxy and temporary
   certificate configured. It makes no account login or order actions. Default
   operation without `-Visible` is headless. This combined helper is syntax
   checked; it has not yet been run as an end-to-end authenticated capture
   session.
3. Sign in **in that app window**, including any SMS/device verification and
   trading-password prompt. Keep passwords, verification codes, session tokens,
   and raw traffic files out of chat.
4. Open holdings, refresh, then open cash details and today's order history.
   Tell me when this is done. I can inspect those captures and test direct
   read-only queries immediately; the current probe requires a fresh capture.

This first stage needs **no new trade**. Its purpose is to establish account
selection, request headers, session handling, balances, position fields, and
read-only access from Python.

## Order-flow evidence

When we need real order-flow evidence, manually submit actions you choose in
the capture app. You choose the instrument, side, quantity, price, and session;
I capture and analyze the resulting requests and order-history responses.
There is no need to perform every case before we inspect the first captures.

| Action to observe | Evidence needed |
| --- | --- |
| A native Market order | Actual market-order instruction, quantity units, treatment of the price field, market/session flags, broker order reference, and execution status. |
| A Limit order | Exact limit-price representation, validity, order reference, and subsequent state. |
| A manual cancellation of an open order | Cancellation identifiers, acknowledgement, and confirmed remaining order state. |
| One-second timed cancellation | The Limit order and manual cancellation captures above provide the two request formats. The one-second timer will be implemented and tested in code; no native FOK capture is required. A manually timed click is not a timing benchmark. |
| An amendment, if amendment support is wanted | Replace-versus-amend identifiers, request fields, and the resulting order reference. |

For each action, a short note in chat is sufficient: the action type, approximate
time, and whether the app reported filled, pending, cancelled, or rejected.
Screenshots are optional. Raw captures already remain in the local private
folder. I cannot submit, amend, or cancel real trades myself.

Single successful trades establish request formats and examples of responses;
they do not establish every edge case. Partial liquidity, lost acknowledgements,
duplicate commands, and cancellation timing remain covered by deterministic
offline tests until matching broker evidence is available. Native FOK is retained
only as a comparison in the simulator, not as the requested API order flow.

## Session lifecycle and scope

Scope clarified by the user on 2026-09-10: Zhuorui permits only one login, and an
already authenticated emulator will be available. New-device phone verification
is outside scope. The API backend should reuse that session and device identity,
with the emulator available for authentication recovery. Orders and holdings must
still use APIs. An app refresh-token request has been captured; direct renewal,
expiry, recovery, and operation with the original emulator stopped remain to be
validated. See [SESSION_HANDLING.md](SESSION_HANDLING.md) for evidence, logout
code mappings from the APK, and the proposed recovery behavior.

The existing repository appears focused on US equities. Confirm whether the API
client should also cover Hong Kong equities or other products, and whether
extended-hours trading is required. Instrument/market identifiers and supported
order types must be mapped separately where their rules differ.

When finished:

```powershell
.\api_research\stop_capture.ps1
```

This restores the original proxy setting and stops capture services. For full
cleanup of the original emulator's temporary capture boot, use
`finish_original_capture.ps1` as described above. Neither helper restarts the
trading listener or makes financial actions.
