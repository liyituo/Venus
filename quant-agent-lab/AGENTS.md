# quant-agent-lab development rules

This is an isolated, offline-first financial engineering demonstration.

- Keep all source, state, reports, audit logs, fixtures, caches, and temporary data under this directory.
- Do not import the parent project's modules or modify files outside this directory.
- Never connect to a live broker, request credentials, or enable real-money execution.
- Use `Decimal` for money, prices, and quantities and timezone-aware UTC timestamps internally.
- Keep strategy, risk, approval, execution, and audit logic structured and deterministic.
- Run the relevant offline tests before marking a checkpoint complete.

The demonstration strategy is not an investment recommendation and makes no profitability claim.
