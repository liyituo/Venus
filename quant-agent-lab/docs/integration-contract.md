# Main Agent and MCP adapter integration contract v1

This contract is a future boundary. This phase does not modify the parent Agent and does not implement authentication or a live broker.

## Allowed capabilities

The future Main Agent may:

- request a daily report;
- retrieve a structured report;
- submit an all/partial/reject approval result;
- request Paper Trading execution;
- query execution status;
- trigger the Kill Switch.

It may not construct a final broker order, bypass risk, alter approval-bound fields, access credentials, or mutate audit history.

## Request conventions

- API prefix: `/api/v1`.
- `request_id` is required for every mutating request in the future authenticated adapter; the local CLI supplies a deterministic or generated request ID.
- Mutating calls should carry an idempotency key. The MVP derives execution idempotency from `report_id`, `approval_id`, and `plan_hash` and stores it in SQLite.
- Timestamps are RFC 3339 with offsets; internal storage normalizes to UTC.
- Decimal fields are serialized as strings, never binary floating-point values.
- Errors contain an application error name/message and stable domain reason codes where a risk check exists.

## Approval binding

An approval is valid only if all of these still match:

`report_id`, `report_version`, `plan_hash`, `account_id`, `strategy_id`, `strategy_version`, `risk_config_version`, approved `order_id` set, and expiry. Any order quantity, price, type, account, report, strategy, or risk change requires a new report, risk evaluation, and approval.

## Status contract

The report status values are defined in `contracts/v1/openapi.yaml`: `DRAFT`, `GENERATED`, `RISK_BLOCKED`, `PENDING_APPROVAL`, `APPROVED`, `PARTIALLY_APPROVED`, `REJECTED`, `EXPIRED`, `EXECUTING`, `PARTIALLY_FILLED`, `FILLED`, `FAILED`, and `CANCELLED`.

## Minimum future adapter

The Main Agent adapter should:

1. authenticate and authorize the caller;
2. forward a unique request ID and idempotency key;
3. display the exact report/plan hash to the user;
4. submit only explicit user approval choices;
5. never transform orders or risk results;
6. surface structured errors without inventing explanations;
7. preserve report, approval, execution, and audit identifiers.

The complete route and payload outline is in [contracts/v1/openapi.yaml](../contracts/v1/openapi.yaml).

## Local MCP Apps adapter

The isolated plugin at `plugins/quant-agent-dashboard` is an adapter, not a second
quantitative engine. Its Node MCP server calls the versioned local FastAPI routes
and returns the backend's structured result. It does not calculate plan hashes,
money, risk, order quantities, approval bindings, or reconciliation.

The adapter exposes these stable tool names:

- `quant_get_dashboard` and `quant_get_report` (read-only views);
- `quant_generate_daily_plan` (report generation);
- `quant_submit_approval` and `quant_reject_plan` (explicit human choices);
- `quant_execute_paper_plan` (fixed `paper` mode only);
- `quant_set_kill_switch` (local safety control);
- `quant_get_execution` and `quant_get_audit_events` (read-only traceability).

The incremental research surface is deliberately separate from the daily trading
surface. It exposes read-only chart/registry reads plus explicit research actions:

- `quant_get_chart_data`, `quant_list_strategies`, and `quant_get_strategy`;
- `quant_validate_strategy` and `quant_save_strategy_draft`;
- `quant_run_strategy_debug`, `quant_get_debug_trace`, `quant_run_backtest`,
  `quant_get_backtest_result`, and `quant_compare_backtests`;
- `quant_promote_strategy_candidate` and `quant_enable_paper_strategy`.

These tools operate on fixed local snapshots and the declaration-only strategy DSL.
They do not call ApprovalService, PaperBroker, or LiveBroker. A strategy must move
through `DRAFT -> VALIDATED -> BACKTESTED -> PAPER_CANDIDATE`; explicit Paper
enablement creates a new version and never replaces an already generated daily plan.
Arbitrary Python is retained as an interface placeholder but returns
`SANDBOX_UNAVAILABLE` until a separately verified OS/container sandbox exists.
The research payload and chart fields are documented in [chart-data-contract.md](chart-data-contract.md)
and [strategy-lab.md](strategy-lab.md).

Every mutating MCP input carries a non-empty `request_id`. The adapter declares
`_meta.ui.resourceUri` as `ui://quant-agent-dashboard/dashboard.html`, serves the
resource as `text/html;profile=mcp-app`, and uses the shared MCP Apps bridge:
`ui/initialize`, `ui/notifications/tool-input`,
`ui/notifications/tool-result`, `tools/call`, and `ui/message`. The UI remains
useful as structured tool text when no component is rendered.
