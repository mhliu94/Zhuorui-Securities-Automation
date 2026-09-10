# Order-flow simulation results

**43 offline tests passed** as of 2026-09-09. No broker requests, live trades,
Kafka messages, or emulator actions were performed for this simulation run.

The tests exercise 18 order/holdings scenarios, 11 account-probe/signing checks,
9 one-second cancellation tests, and 5 exact-decimal signing regressions added
after analyzing the user-submitted Limit order. These validate the local model and read-only
guards. They do **not** prove Zhuorui's authenticated API behavior or order-type
wire values.

## Current API requirements

Clarified by the user on 2026-09-09:

- Market orders must be native broker market orders. The earlier UI limit-price
  substitution does not carry over to the API backend.
- The API replacement for the old FOK command is a limit order with cancellation
  targeted one second after submission. Native FOK is not required. Partial
  fills are preserved, and a completed order is not cancelled.
- The one-second deadline is measured from request dispatch. A late broker
  acknowledgement does not start a new one-second wait. When order identity is
  uncertain, reconciliation precedes cancellation; no blind resubmission occurs.

`simulated_timed_cancel.py` exercises that strategy against the offline model.
Its nine tests cover exact timing, acknowledgement latency, lost acknowledgements,
unknown identity, partial fills, terminal states, and separation from native
market orders. This does not implement or execute a live broker cancellation.
The table below retains the original market/limit/native-FOK comparison fixtures.

## Main finding: the existing FOK flow is not native FOK

The current script submits a limit order, waits three seconds, then attempts
cancellation. The regression test calls that existing method with a simulated
trader and a mocked timer; it never invokes ADB or a real broker.

With a request for three synthetic shares and only two available within the
limit, this flow finishes with **two shares filled** and the remainder cancelled.
The native FOK model finishes with **zero shares filled**. Native FOK requires
immediate execution of the entire quantity or cancellation; a later cancellation
cannot undo completed fills. See the [SEC order-type bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-14).

The API implementation must identify the requested behavior as timed
cancellation rather than promise native FOK guarantees. The user has selected
a one-second delay for that strategy. No live order behavior was changed here.

## Synthetic examples

Every example starts with USD 1,000 and no position in the fictional symbol
`TEST`. Fees are zero. Numbers below are test fixtures, not trade suggestions.

| Scenario | Requested | Filled | Final order state | Cash afterward |
| --- | ---: | ---: | --- | ---: |
| Market: 1 share at 10, then 2 at 11 | 3 | 3 | Filled | 968 |
| Limit at 10: 2 available at 10, remainder at 11 | 3 | 2 | Partially filled; remainder open | 980 |
| Native FOK at 10, same insufficient depth | 3 | 0 | Cancelled | 1,000 |
| Native FOK at 10.50: 1 at 10, 2 at 10.50 | 3 | 3 | Filled | 969 |
| Submit-limit-then-cancel at 10, insufficient depth | 3 | 2 | Cancelled; completed fills remain | 980 |

Machine-readable fixtures and outcomes: [simulation-results.json](simulation-results.json).

## Covered behaviors

- Market orders consume multiple price levels; buys and sells update cash and
  position quantities. The model cancels an unfilled market remainder.
- Limit orders respect the price boundary and may remain open or partially
  filled. Native FOK checks the entire eligible quantity before changing the
  simulated book or account.
- Cancellation preserves completed fills, repeated cancellation is harmless,
  and cancellation after full fill cannot reverse the trade.
- A lost acknowledgement is reconciled by inspecting the existing simulated
  order, without a second submission. Repeated client commands cannot double
  the simulated fill; reusing an ID with different parameters fails.
- Holdings reflect weighted average buy cost; a partial sale preserves the
  remaining average cost and a full sale removes the position.
- Invalid quantities/prices, insufficient cash, and unsupported short sales
  do not mutate the simulated account or consume liquidity.

Simulation limits: long-only shares, one currency, zero fees, immediate cash
accounting, deterministic liquidity, and no margin, settlement timing, exchange
sessions, reservations for open orders, or automatic future matching of resting
orders. Simulated client-ID lookup is **not** evidence that Zhuorui supports
broker-side idempotency. These assumptions must be replaced by observed broker
rules where they matter to the real implementation.

## Read-only holdings probe

`probe_holdings_api.py` is prepared for authenticated capture validation. It
permits only exact holdings, funds, and asset-detail paths observed successfully
in a captured account session. It verifies the captured signature locally before
preparing a fresh request. `--send` additionally requires a request captured
within ten minutes and permits one direct query, with no retries or redirects.

Its tests reject order-entry, amendment, cancellation, and login paths; altered
hosts/ports/schemes/methods; query-string additions; missing tokens; stale or
future timestamps; and unsuccessful captured responses. Default operation is
offline and makes no request.

The current saved capture has no authenticated holdings request. Running the
probe returned **not ready**, as expected, and sent nothing. Real cash/holdings
results and session renewal remain unverified.

Run from the project root:

```powershell
.\api_research\.venv\Scripts\python.exe -m unittest discover -s api_research -p 'test_*.py' -v
.\api_research\.venv\Scripts\python.exe .\api_research\probe_holdings_api.py
```

The existing public-query signing probe was also rechecked offline after the
signer was shared with the account probe; its captured signature still matches
exactly.

## Evidence needed before implementing real API order flows

| Flow | Required capture and comparison |
| --- | --- |
| Holdings | Authenticated refresh; cash and security rows; market IDs; pagination; total/available quantity; average-cost meaning; empty-account and expired-session responses. |
| Market | A user-submitted app market order; actual `entrustProp`, price-field behavior, time-in-force, session flags, broker reference, and resulting fills. The existing script instead synthesizes a through-market limit order. |
| Limit | A user-submitted limit order; price precision, quantity units, validity/session flags, acceptance, partial fills, and any user-performed amendment/cancellation. |
| One-second cancellation | Capture a user-submitted Limit order and user-performed cancellation. Implement the one-second deadline locally with order-identity reconciliation. A native FOK capture is not required for the user's requested strategy. |
| Session and outcomes | Login/trading unlock, token expiry, device binding, order-history status fields, broker reference correlation, and recovery after an uncertain response. |

For any later real-order capture, the user chooses and submits the trade and
performs any amendment or cancellation. The research tools observe traffic and
query account information; they do not execute those financial actions.
