# Quant Center integration

## Baseline

The parent Agent is the repository root; its desktop GUI is `src/chat.py`.
The isolated quantitative project is `quant-agent-lab/` and has no nested Git
repository. The current baseline is 29 Python tests and 5 Node
MCP/UI/standalone contract tests. On the local Windows validation host, the
parent project's pre-existing `process_stop_test.py` exceeded the observation
window; the quant-specific controller, GUI contract and real-service E2E tests
all passed independently.

The parent Agent reserves `127.0.0.1:8000` for the screen daemon and
`127.0.0.1:8001` for the LLM server. The Quant Center uses only:

- backend: `http://127.0.0.1:8014`
- standalone GUI: `http://127.0.0.1:4173`

## Boundary

`src/quant_integration.py` is a thin controller. It does not import
`quant_agent`, open the quantitative SQLite database, call a broker, generate
reports, submit approvals or execute orders. It validates loopback URLs,
discovers a Python runtime by importing the isolated package, starts the
backend with an argument array, starts the standalone Node host with an
argument array, polls `/api/v1/health` and `/healthz`, and opens only
`/#/dashboard` in the browser.

Child processes receive a minimal runtime environment and write logs to
`quant-agent-lab/var/integration/`. Only exact `Popen` objects created by the
current controller may be terminated on parent shutdown. An already healthy
external service is reused and is not owned.

The toolbar button is navigation/lifecycle only. Clicking it does not generate
a daily plan, approve a plan, or execute Paper Trading. The quant backend
continues to own all risk, approval, execution and audit authority; LiveBroker
remains disabled.

## Standalone host

`quant-agent-lab/plugins/quant-agent-dashboard/standalone/server.mjs` is the
formal local host. It binds only to `127.0.0.1`, serves the built UI, proxies
only the existing MCP tool contract through `callTool`, reports `/healthz`,
and uses the real backend URL from `QUANT_AGENT_BACKEND_URL`. It does not use
Harness scenario payloads. Routes include `/`, `/#/dashboard`, `/#/chart`,
`/#/strategy-lab`, `/#/backtests`, `/#/risk` and `/#/audit`; the built UI
remains the shared MCP App asset. Opening the center performs read-only
dashboard/chart/strategy loads only. It does not call report generation,
approval or execution tools.

The controller does not seed data or mutate quant runtime state. A fresh
checkout should be prepared with the quant project's documented
`python -m quant_agent seed-demo` command before opening the chart; existing
local fixtures are reused unchanged.

## Configuration

The new fields are optional and backward-compatible. Missing fields use the
safe defaults shown in `chat_config.example.json`. URLs reject credentials,
query strings, non-loopback hosts, invalid ports and ports 8000/8001. No token,
approval, account or broker data is stored in these fields.

## Verified integration checks

- parent thin-controller checks: `python tests\quant_integration_test.py`
- real local E2E: `python tests\quant_integration_e2e_test.py`
- quant Python suite: `PYTHONPATH=src pytest -q` — 29 passed
- plugin MCP/UI/standalone contract suite: `node --test mcp-server/tests/contract.test.mjs ui/tests/ui-contract.test.mjs standalone/tests/host-contract.test.mjs` — 5 passed
- plugin build: `node scripts\build.mjs`

The real E2E starts the two services only on 8014/4173, verifies
`/api/v1/health`, `/healthz`, `/api/connection` and a structured
`quant_get_dashboard` bridge call, checks that a second open reuses the same
owned PIDs, and stops only those exact children. It does not generate a new
report or execute an order.

## Visual acceptance

The real standalone page was opened in the in-app browser and checked through
the Dashboard, K-line/signals and Strategy Lab routes. Captured artifacts are:

- [`artifacts/quant-integration/quant-dashboard-opened.png`](../artifacts/quant-integration/quant-dashboard-opened.png)
- [`artifacts/quant-integration/main-gui-returned.png`](../artifacts/quant-integration/main-gui-returned.png)

The requested Tk desktop screenshots for the toolbar/start/error states could
not be captured in this environment: all available Python Tk builds failed to
create a root window because the local Tcl/Tk installation reports a missing
or incompatible `init.tcl`. No synthetic desktop screenshots were substituted.
