# Session handling for the API backend

Updated 2026-09-10. This document separates the implemented session-recovery
contract from its live validation evidence.

Implementation update: `zhuorui/api` now provides direct import from the supported
root-readable emulator or a recent successful capture, Windows DPAPI storage,
direct account queries, periodic Kafka publishing and same-device password
re-login after `000102`/`000112`. The captured token-refresh endpoint is not used.
See [the API guide](../docs/api.md) for current configuration and operating steps.

## User's operating requirements

- Zhuorui permits one login at a time. A login on another device invalidates the
  existing device's login. The user supplied this constraint.
- The user will provide an already authenticated emulator session. New-device
  enrollment and phone verification are outside the requested automation scope.
- Orders, cancellation and holdings must use HTTP APIs. An emulator may provide
  authentication; it must not be on the path for every trading operation.
- On logout detected from 09:00 inclusive to 16:00 exclusive Beijing time, wait
  five minutes from the first detection before login. Outside those hours,
  attempt recovery as soon as the current command finishes.

## What the local evidence establishes

Direct Windows holdings and cash queries succeeded using the captured app token
and device headers. They sent no login requests, used no ADB calls, and bypassed
the proxy. The original emulator was running during those probes, so independence
from an app heartbeat or long-term renewal is not yet established.

Direct emulator import was also verified on the installed 3.1.5 build: all eleven
constructed headers matched captured app traffic, and direct account/holdings
queries succeeded. The importer reads existing storage, including the app-scoped
Android ID; it never logs in. See [evidence](emulator-session-evidence.json).

The app called `POST /as_user/api/user_account/v1/refresh_token` with its token
header and a body containing only `timeStamp` and `sign`. The successful
response's `data` was a string equal to the input token. The next authenticated
request used that token. Its signature was reproduced offline. This one capture
does not prove that refresh always preserves the token, extends it for a known
duration, or can recover an already revoked token. The token is opaque, not a
three-part JWT; its lifetime must not be guessed from its contents.

Static inspection of the installed APK found these branches in
`base2app/network/interceptor/TokenInterceptor.intercept`:

| Application code | App handling | Evidence limit |
| --- | --- | --- |
| `000102` | Calls `PersonalService.tokenOverdue`; clears local login state and requests login. | Not yet observed in a captured expired-session response. |
| `000112` | Calls `PersonalService.loginOverdue`; its implementation explicitly labels this a login elsewhere, clears local login state, and reports the device offline. | Observed by the direct listener on 2026-09-10; scheduled password recovery and resumed holdings publication succeeded. |
| Other trade-auth codes | Delegates to `TradeService` authorization handling. | Exact invalid-trade-auth code set still needs inspection/validation. |

The app's `LocalAccountConfig.isLogin()` only checks for a nonempty local token.
An apparently signed-in UI is not sufficient evidence of a valid server session.
The successful `current_auth_info` response also had no populated `data` before
the trading-password unlock, then populated account data afterwards. Ordinary
login and trading authorization therefore require separate checks.

The same emulator's password login and trading unlock were captured successfully.
The password-login MD5 field has now been reproduced from that flow and is used
by the direct login implementation. Trading-password SM2 transformation has been
derived and automatic trading unlock is now implemented using the shared
`trade_password` configuration. The new unlock implementation has offline
validation; no live unlock request has been sent by its tests. Neither captured
login nor offline recovery tests alone establish end-to-end handling of every
expired-session, displaced-login or verification response on the live account.

## Implemented recovery flow

1. Import a consistent set of session token, account identity, and device headers
   from the existing app session. Validate it with a signed read-only account
   query. Never mix an older token with a newer login's identity. Establish the
   expected account before permitting order commands.
2. Check HTTP status and application codes on every response. Holdings publication
   runs every 30 seconds, also detecting confirmed session loss while idle.
   `000102` and `000112` schedule recovery. Timeouts, generic HTTP errors, ordinary
   order rejection and empty holdings are not themselves logout signals.
3. Record the first detection and apply the requested Beijing-time policy.
   Detection at 15:59 remains due at 16:04. Repeated errors do not postpone it.
   Persist the schedule beside the encrypted session as `session.recovery.json`
   for a cache named `session.dpapi`; restart retains deadlines and blocked state.
4. Run password login on the command thread between commands, using the saved
   account/device headers and shared `login.phone`, `login.password` and
   `login.phone_area` (default `"86"`). The default `api.auto_login_enabled` is
   true. Validate returned phone, broker user and device binding, then atomically
   save the new token under DPAPI. Routine recovery performs no emulator calls.
5. A temporary transport failure retries after `api.login_retry_seconds` (default
   300). Phone-verification code `010007`, other explicit login rejections,
   credential/setup failures and unexpected identities pause automatic attempts
   for review. Existing device enrollment/phone verification remains manual.
   Zhuorui's one-login rule still applies to a successful recovery.
6. Request fresh holdings after successful login. Do not replay commands rejected
   during recovery or clear unknown journal outcomes. A timed-out submission is
   never resent automatically. A one-second cancellation deadline cannot be
   guaranteed while authentication is unavailable; reconcile the original order.

Trading authorization stays separate. A valid ordinary login with locked trading
triggers an on-demand encrypted trading-password request before order/cancellation
dispatch. The runtime reads the existing `trade_password` setting, sends the
account's `clientId`, and verifies user/account authorization after the response.
Failures pause password attempts until the listener restarts or an unlocked
response is verified. Ordinary login recovery never replays a rejected command
or resets that pause. See [API recovery](../docs/api.md) for details.

Session exchange is local and protected. Direct import from the supported
root-readable emulator and capture import are implemented as explicit commands.
The existing DPAPI cache is preferred at startup; a logged-out emulator does not
replace it. Optional `api.auto_import_session` (default false) can import from a
valid root-readable emulator when needed. It is separate from automatic password
login. Continuous private traffic recording is not needed for API operation.

## Emulator runtime requirement

After initial import, API reads, writes and routine password re-login have no
emulator dependency. The emulator remains the route for initial device enrollment,
phone verification and manual session re-import. Routine trading-password unlock
now uses the API. The emulator can
be stopped during ordinary API operation; stopping it must not mean signing out
of the account. Keep the UI listener separate and do not run the same trading
commands through both backends.

The original investigation used captured traffic and APK analysis without
replaying login, refresh or trade requests. On 2026-09-10, the direct listener
detected `000112` at 11:24:24 Beijing, retained the five-minute deadline, logged
in successfully at 11:29:25 and published holdings immediately afterward.
Subsequent periodic publications were acknowledged about 30 seconds apart.
See [login recovery evidence](login-recovery-evidence.json). No orders were sent.
Long-running renewal, other expiry cases and trading authorization lifetime
still need further observation.
