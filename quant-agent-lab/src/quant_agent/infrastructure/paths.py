from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def var_dir(self) -> Path:
        return self.root / "var"

    @property
    def state_db(self) -> Path:
        return self.var_dir / "state.sqlite3"

    @property
    def data_dir(self) -> Path:
        return self.var_dir / "data"

    @property
    def reports_dir(self) -> Path:
        return self.var_dir / "reports"

    @property
    def audit_dir(self) -> Path:
        return self.var_dir / "audit"

    @property
    def audit_log(self) -> Path:
        return self.audit_dir / "events.jsonl"

    @property
    def strategies_dir(self) -> Path:
        return self.var_dir / "strategies"

    @property
    def research_dir(self) -> Path:
        return self.var_dir / "research"

    @property
    def backtests_dir(self) -> Path:
        return self.research_dir / "backtests"

    @property
    def debug_dir(self) -> Path:
        return self.research_dir / "debug"

    @property
    def research_audit_log(self) -> Path:
        return self.research_dir / "events.jsonl"

    @classmethod
    def default(cls) -> ProjectPaths:
        return cls(PROJECT_ROOT)

    def ensure(self) -> None:
        for directory in (
            self.config_dir,
            self.var_dir,
            self.data_dir,
            self.reports_dir,
            self.audit_dir,
            self.strategies_dir,
            self.research_dir,
            self.backtests_dir,
            self.debug_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
