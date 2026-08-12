# Threat model

The MVP assumes local files and the future host Agent may be untrusted or buggy. The safety posture is deny-by-default and simulation-only.

| Threat | Control | Evidence or residual risk |
|---|---|---|
| Unauthorized trading | No live broker implementation; execution requires explicit approval and `--paper`/`paper` mode | A future live adapter must be separately reviewed and remain disabled by default |
| Approval replay | Approval binds report version, plan hash, account, strategy, risk config, order IDs, and expiry | Storage access is local; an operator with database write access can still tamper with local state |
| Duplicate orders | Stable `client_order_id`, broker idempotency, execution idempotency key | Cross-process locking is intentionally minimal for the offline MVP |
| Stale or poisoned data | Freshness, timezone, positive price, OHLC, currency, volume, and account consistency validation | Fixtures are trusted after validation; provenance is local-source metadata |
| Strategy version drift | Strategy ID/version and code version are part of the plan and approval binding | Version governance must be extended before production use |
| Risk config tampering | Versioned external config is bound into the plan and approval | File permissions and signed config are outside MVP scope |
| Credential leakage | No credentials are accepted or accessed; audit summaries are bounded | Host process/logging policy is outside this project |
| Audit leakage or mutation | Append-only SQLite event rows and JSONL mirror; no secret fields in event model | Local users with filesystem access can delete files; production would require WORM/remote storage |
| Broker disconnection | No network broker; PaperBroker is deterministic and local | Live connectivity/retry behavior is not implemented |
| Partial fills | Broker returns explicit fill quantities, remaining orders, and reconciliation result | Complex settlement/corporate actions are outside scope |
| Clock and timezone errors | Aware timestamps, UTC normalization, freshness/future checks, configured local display zone | Clock source trust is not independently attested |
| Fabricated explanations | Structured results are the source of truth; deterministic narrative only describes them | Future narrative providers must not mutate facts |
| Main Agent permission expansion | Integration contract exposes report, approval result, paper execution, status, and kill switch only | Authentication/authorization is a documented placeholder for future integration |

## Failure posture

Failures retain cash, avoid broker calls, return stable error/reason codes, and create an audit record where an application service is available. A risk block cannot be bypassed by approval because approval eligibility is computed from risk-allowed order IDs.
