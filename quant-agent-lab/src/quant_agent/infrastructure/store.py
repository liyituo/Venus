from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quant_agent.domain.codec import (
    approval_from_dict,
    audit_from_dict,
    execution_from_dict,
    report_from_dict,
)
from quant_agent.domain.models import Approval, AuditEvent, DailyReport, ExecutionResult, to_dict


class SQLiteStore:
    def __init__(self, db_path: Path, audit_log: Path | None = None) -> None:
        self.db_path = db_path
        self.audit_log = audit_log or db_path.with_name("events.jsonl")

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_report ON approvals(report_id);
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    def set_value(self, key: str, value: Any) -> None:
        with closing(self._connection()) as connection:
            connection.execute(
                "INSERT INTO kv(key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
            )
            connection.commit()

    def get_value(self, key: str, default: Any = None) -> Any:
        with closing(self._connection()) as connection:
            row = connection.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def save_report(self, report: DailyReport) -> None:
        payload = json.dumps(to_dict(report), ensure_ascii=False, sort_keys=True)
        with closing(self._connection()) as connection:
            connection.execute(
                "INSERT INTO reports(report_id, status, payload_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(report_id) DO UPDATE SET status=excluded.status, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (report.report_id, report.status.value, payload, self._now()),
            )
            connection.commit()

    def get_report(self, report_id: str) -> DailyReport | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM reports WHERE report_id = ?", (report_id,)
            ).fetchone()
        return None if row is None else report_from_dict(json.loads(row["payload_json"]))

    def get_latest_report(self) -> DailyReport | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM reports ORDER BY updated_at DESC, report_id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else report_from_dict(json.loads(row["payload_json"]))

    def save_approval(self, approval: Approval) -> None:
        payload = json.dumps(to_dict(approval), ensure_ascii=False, sort_keys=True)
        with closing(self._connection()) as connection:
            connection.execute(
                "INSERT INTO approvals(approval_id, report_id, payload_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(approval_id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (approval.approval_id, approval.report_id, payload, self._now()),
            )
            connection.commit()

    def get_approval(self, approval_id: str) -> Approval | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return None if row is None else approval_from_dict(json.loads(row["payload_json"]))

    def get_latest_approval(self, report_id: str) -> Approval | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM approvals WHERE report_id = ? ORDER BY updated_at DESC LIMIT 1",
                (report_id,),
            ).fetchone()
        return None if row is None else approval_from_dict(json.loads(row["payload_json"]))

    def save_execution(self, execution: ExecutionResult) -> None:
        payload = json.dumps(to_dict(execution), ensure_ascii=False, sort_keys=True)
        with closing(self._connection()) as connection:
            connection.execute(
                "INSERT INTO executions(execution_id, report_id, idempotency_key, payload_json, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(execution_id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                (
                    execution.execution_id,
                    execution.report_id,
                    execution.idempotency_key,
                    payload,
                    self._now(),
                ),
            )
            connection.commit()

    def get_execution(self, execution_id: str) -> ExecutionResult | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        return None if row is None else execution_from_dict(json.loads(row["payload_json"]))

    def get_execution_by_key(self, idempotency_key: str) -> ExecutionResult | None:
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM executions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return None if row is None else execution_from_dict(json.loads(row["payload_json"]))

    def append_audit(self, event: AuditEvent) -> None:
        payload = json.dumps(to_dict(event), ensure_ascii=False, sort_keys=True)
        with closing(self._connection()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO audit_events(event_id, timestamp, payload_json) VALUES (?, ?, ?)",
                (event.event_id, to_dict(event.timestamp), payload),
            )
            connection.commit()
        if cursor.rowcount == 1:
            with self.audit_log.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")

    def list_audit(
        self, report_id: str | None = None, limit: int | None = None
    ) -> list[AuditEvent]:
        with closing(self._connection()) as connection:
            if report_id is None:
                query = "SELECT payload_json FROM audit_events ORDER BY timestamp, event_id"
                parameters: tuple[Any, ...] = ()
            else:
                query = (
                    "SELECT payload_json FROM audit_events "
                    "WHERE json_extract(payload_json, '$.report_id') = ? "
                    "ORDER BY timestamp, event_id"
                )
                parameters = (report_id,)
            if limit is not None:
                query += " LIMIT ?"
                parameters = (*parameters, max(0, limit))
            rows = connection.execute(query, parameters).fetchall()
        return [audit_from_dict(json.loads(row["payload_json"])) for row in rows]

    def set_kill_switch(self, enabled: bool, reason: str, actor: str) -> None:
        self.set_value(
            "kill_switch",
            {"enabled": enabled, "reason": reason, "actor": actor, "updated_at": self._now()},
        )

    def kill_switch(self) -> dict[str, Any]:
        value = self.get_value("kill_switch", {"enabled": False, "reason": "default-off"})
        return value if isinstance(value, dict) else {"enabled": False, "reason": "invalid-state"}

    def reset_runtime(self) -> None:
        """Reset only project-local demo state; source/configuration are untouched."""
        with closing(self._connection()) as connection:
            connection.executescript(
                "DELETE FROM reports; DELETE FROM approvals; DELETE FROM executions; DELETE FROM audit_events; DELETE FROM kv;"
            )
            connection.commit()
        if self.audit_log.exists():
            self.audit_log.unlink()
