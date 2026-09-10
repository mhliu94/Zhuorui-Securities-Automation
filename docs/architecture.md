# Project structure

`zhuorui` contains application implementations. Root Python and PowerShell files
are compatibility entry points for existing commands and dashboard controls.

| Area | Responsibility |
| --- | --- |
| `zhuorui/common` | Configuration parsing, account labels and credential lookup. |
| `zhuorui/ui` | Android navigation, UI orders and the existing Kafka listener. |
| `zhuorui/api` | Signing, encrypted sessions, scheduled login recovery, Kafka commands, direct orders and account publishing. |
| `zhuorui/capture` | Portable capture settings, image/path discovery and boot-evidence validation. |
| `zhuorui/monitor` | Dashboard server and listener/emulator controls. |
| `scripts/windows` | Actual PowerShell process and certificate-management scripts. |
| `tests` | Application tests with synthetic API sessions and responses. |
| `api_research` | Capture helpers, protocol evidence and broker simulations. |
| `docs` | Operating guides and component boundaries. |

Runtime and research probes use the same signing implementation, including
decimal-scale behavior. API account queries do not import the UI backend or
capture libraries. Capture import alone loads mitmproxy. Direct emulator import
uses read-only ADB access and the validated app storage/header adapter, then
normal account queries use only the encrypted cache and HTTPS.

The UI implementation was relocated with shared configuration imports and an
adjusted checkout-relative path. Its root import resolves to the implementation
module, preserving existing test patches. Dashboard assets remain at
`monitor_web` for the already running dashboard. Root launchers forward parameters
to the Windows helpers, which retain root-relative config, log and PID paths.

Private configuration, process state, emulator data and active captures were not
relocated. Ignored `runtime` holds encrypted sessions and local migration backups.
The checked-in configuration example uses placeholders.

API transport exposes named account reads, native Market/Limit orders, cancellation
and password login. Offline order planning sends nothing. The Kafka listener uses
a durable command journal to prevent duplicate submission and pauses new orders
when a submission outcome is unknown. Holdings publication runs independently of
the one-second timed cancellation; login recovery runs between complete commands.
The dashboard controls this API listener and disables scheduled emulator restarts
in API mode.
