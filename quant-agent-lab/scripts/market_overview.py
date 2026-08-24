"""纸面持仓 + 大盘行情速览（多数据源）。"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_agent.data.cn_fetchers import fetch_cn_daily, normalize_cn_sources
from quant_agent.infrastructure.config import load_demo_config


def main() -> int:
    import akshare as ak

    cfg = load_demo_config(ROOT / "config")
    cn_sources = normalize_cn_sources(cfg.market_data.cn_sources or None)

    account_path = ROOT / "var/data/account_snapshot.json"
    account = json.loads(account_path.read_text(encoding="utf-8"))
    positions = account["positions"]
    cash = Decimal(account["cash"])

    # 沪深300（新浪指数仍可用）
    idx = ak.stock_zh_index_daily(symbol="sh000300")
    idx_tail = idx.tail(5)
    print("=== 沪深300（最近 5 个交易日）===")
    prev_close = None
    for _, row in idx_tail.iterrows():
        chg = ""
        if prev_close is not None:
            chg = f"  ({row['close'] / prev_close - 1:+.2%})"
        print(f"  {row['date']}: {row['close']:.2f}{chg}")
        prev_close = row["close"]
    latest_idx_date = str(idx_tail.iloc[-1]["date"])
    latest_idx_close = float(idx_tail.iloc[-1]["close"])

    print(f"\n=== 纸面账户（行情截至最新日线，sources={','.join(cn_sources)}）===")
    print(f"  现金: {float(cash):,.2f} CNY")
    print(f"  持仓: {len(positions)} 只")

    rows: list[dict] = []
    total_mv = Decimal("0")
    total_cost = Decimal("0")
    day_ups = day_downs = 0
    day_changes: list[float] = []
    source_counts: dict[str, int] = {}

    for p in positions:
        sym = p["symbol"]
        qty = Decimal(p["quantity"])
        avg = Decimal(p["average_price"])
        frame = fetch_cn_daily(sym, sources=cn_sources)
        source_counts[frame.source] = source_counts.get(frame.source, 0) + 1
        bars = frame.rows
        latest = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else None
        price = Decimal(str(latest["close"]))
        trade_date = str(latest["date"])
        day_chg = 0.0
        if prev is not None:
            day_chg = float(latest["close"] / prev["close"] - 1)
            day_changes.append(day_chg)
            if day_chg > 0:
                day_ups += 1
            else:
                day_downs += 1
        mv = qty * price
        cost = qty * avg
        pnl = mv - cost
        total_mv += mv
        total_cost += cost
        rows.append(
            {
                "symbol": sym,
                "date": trade_date,
                "source": frame.source,
                "price": float(price),
                "avg": float(avg),
                "day_chg": day_chg,
                "pnl": float(pnl),
                "pnl_pct": float(pnl / cost * 100) if cost else 0.0,
                "mv": float(mv),
            }
        )

    equity = cash + total_mv
    init = Decimal("500000")
    total_pnl = equity - init
    avg_day = sum(day_changes) / len(day_changes) * 100 if day_changes else 0.0

    print(f"  持仓市值: {float(total_mv):,.2f}")
    print(f"  总权益: {float(equity):,.2f}")
    print(f"  总盈亏: {float(total_pnl):+,.2f} ({float(total_pnl/init*100):+.2f}%)")
    if rows:
        print(f"  数据日期: {rows[0]['date']}（指数: {latest_idx_date}）")
    print(f"  数据源分布: {source_counts}")
    print(f"  持仓当日涨跌: 涨 {day_ups} / 跌 {day_downs}，等权均值 {avg_day:+.2f}%")

    print("\n=== 持仓明细（按当日涨跌排序）===")
    print(f"{'代码':<8} {'现价':>8} {'成本':>8} {'当日':>7} {'浮盈':>10} {'市值':>10} {'源':>5}")
    for r in sorted(rows, key=lambda x: x["day_chg"], reverse=True):
        print(
            f"{r['symbol']:<8} {r['price']:>8.2f} {r['avg']:>8.2f} "
            f"{r['day_chg']*100:>+6.2f}% {r['pnl']:>+10.2f} {r['mv']:>10.0f} {r['source']:>5}"
        )

    gainers = sorted(rows, key=lambda x: x["day_chg"], reverse=True)[:3]
    losers = sorted(rows, key=lambda x: x["day_chg"])[:3]
    print("\n=== 当日强势 / 弱势 ===")
    print("  涨:", ", ".join(f"{g['symbol']} {g['day_chg']*100:+.1f}%" for g in gainers))
    print("  跌:", ", ".join(f"{l['symbol']} {l['day_chg']*100:+.1f}%" for l in losers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
