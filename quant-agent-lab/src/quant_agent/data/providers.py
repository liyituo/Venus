from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from quant_agent.domain.codec import account_from_dict, market_snapshot_from_dict
from quant_agent.domain.models import AccountSnapshot, MarketBar, MarketSnapshot, Position, to_dict

UTC = UTC


class FileDataProvider:
    """Offline JSON provider. It never reaches the network."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def market_path(self) -> Path:
        return self.data_dir / "market_snapshot.json"

    @property
    def account_path(self) -> Path:
        return self.data_dir / "account_snapshot.json"

    def load_market(self) -> MarketSnapshot:
        return market_snapshot_from_dict(json.loads(self.market_path.read_text(encoding="utf-8")))

    def load_account(self) -> AccountSnapshot:
        return account_from_dict(json.loads(self.account_path.read_text(encoding="utf-8")))

    def save_market(self, snapshot: MarketSnapshot) -> None:
        self.market_path.write_text(
            json.dumps(to_dict(snapshot), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def save_account(self, account: AccountSnapshot) -> None:
        self.account_path.write_text(
            json.dumps(to_dict(account), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


class CsvMarketDataProvider:
    """Small CSV adapter for local fixtures with no network fallback."""

    def __init__(
        self, csv_path: Path, snapshot_id: str = "csv-snapshot", source: str = "local-csv"
    ) -> None:
        self.csv_path = csv_path
        self.snapshot_id = snapshot_id
        self.source = source

    def load_market(self) -> MarketSnapshot:
        bars: list[MarketBar] = []
        with self.csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                bars.append(
                    MarketBar(
                        symbol=row["symbol"],
                        timestamp=datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                        currency=row.get("currency", "USD"),
                        timeframe=row.get("timeframe", "1d"),
                        source=self.source,
                        is_synthetic=row.get("is_synthetic", "false").lower() == "true",
                        session=row.get("session", "regular"),
                        snapshot_id=self.snapshot_id,
                    )
                )
        if not bars:
            raise ValueError("CSV fixture contains no market bars")
        return MarketSnapshot(
            snapshot_id=self.snapshot_id,
            as_of=max(bar.timestamp for bar in bars),
            source=self.source,
            bars=tuple(bars),
        )


def seed_demo_data(data_dir: Path) -> tuple[MarketSnapshot, AccountSnapshot]:
    """Write deterministic demo data and return the exact snapshots written."""

    as_of = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
    pattern = (
        Decimal("-1.2"),
        Decimal("0.4"),
        Decimal("1.5"),
        Decimal("0.2"),
        Decimal("-0.8"),
        Decimal("1.1"),
    )
    closes = {
        "AAPL": tuple(
            Decimal("97") + Decimal(index) * Decimal("0.23") + pattern[index % len(pattern)]
            for index in range(40)
        ),
        "MSFT": tuple(
            Decimal("202") - Decimal(index) * Decimal("0.11") - pattern[index % len(pattern)]
            for index in range(40)
        ),
        "DEMO": tuple(
            Decimal("50") + pattern[index % len(pattern)] / Decimal("4") for index in range(40)
        ),
    }
    bars: list[MarketBar] = []
    for symbol, values in closes.items():
        for index, close in enumerate(values):
            price = Decimal(close)
            timestamp = datetime(2026, 7, 2, 9, 30, tzinfo=UTC) + __import__("datetime").timedelta(
                days=index
            )
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=price,
                    high=price + Decimal("1"),
                    low=price - Decimal("1"),
                    close=price,
                    volume=Decimal("100000") + Decimal(index * 1000),
                    currency="USD",
                    timeframe="1d",
                    source="deterministic-offline-fixture",
                    is_synthetic=True,
                    session="regular",
                    snapshot_id="demo-market-2026-08-11-v1",
                )
            )
    market = MarketSnapshot(
        snapshot_id="demo-market-2026-08-11-v1",
        as_of=as_of,
        source="deterministic-offline-fixture",
        bars=tuple(bars),
    )
    account = AccountSnapshot(
        account_id="paper-demo-account",
        as_of=as_of,
        cash=Decimal("5000"),
        equity=Decimal("5980"),
        currency="USD",
        positions=(
            Position(
                symbol="MSFT",
                quantity=Decimal("5"),
                average_price=Decimal("195"),
                market_price=Decimal("196"),
                currency="USD",
            ),
        ),
        status="VERIFIED",
        source="deterministic-offline-fixture",
    )
    provider = FileDataProvider(data_dir)
    provider.save_market(market)
    provider.save_account(account)
    return market, account
