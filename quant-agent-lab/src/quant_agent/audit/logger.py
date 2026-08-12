from __future__ import annotations

from datetime import datetime

from quant_agent.domain.models import AuditEvent, canonical_hash
from quant_agent.infrastructure.clock import Clock
from quant_agent.infrastructure.store import SQLiteStore


class AuditLogger:
    def __init__(self, store: SQLiteStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def record(
        self,
        event_type: str,
        *,
        actor: str = "system",
        request_id: str = "system",
        report_id: str | None = None,
        order_id: str | None = None,
        before_state: str | None = None,
        after_state: str | None = None,
        reason_code: str = "OK",
        input_summary: str = "",
        result_summary: str = "",
        timestamp: datetime | None = None,
    ) -> AuditEvent:
        event_time = timestamp or self.clock.now()
        event_identity = {
            "event_type": event_type,
            "request_id": request_id,
            "report_id": report_id,
            "order_id": order_id,
            "timestamp": event_time,
        }
        event_id = f"evt_{canonical_hash(event_identity)[:24]}"
        event = AuditEvent(
            event_id=event_id,
            timestamp=event_time,
            event_type=event_type,
            actor=actor,
            request_id=request_id,
            report_id=report_id,
            order_id=order_id,
            before_state=before_state,
            after_state=after_state,
            reason_code=reason_code,
            input_summary=input_summary[:500],
            result_summary=result_summary[:500],
        )
        self.store.append_audit(event)
        return event
