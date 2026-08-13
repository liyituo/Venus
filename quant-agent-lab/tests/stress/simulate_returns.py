"""快速模拟收益：模拟市场快进 N 天，LLM 每日出信号 → 风控 → 纸面执行。

输出：初始/最终权益、总收益、收益率、每日权益曲线、交易统计。
用法：python tests/stress/simulate_returns.py [--days 20] [--symbols AAPL MSFT]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_agent.data.simulated_provider import SimulatedMarketProvider  # noqa: E402
from quant_agent.infrastructure.config import LlmConfig  # noqa: E402
from quant_agent.infrastructure.clock import FrozenClock  # noqa: E402
from quant_agent.infrastructure.paths import ProjectPaths  # noqa: E402
from quant_agent.llm.client import LlmClient  # noqa: E402
from quant_agent.llm.rag_client import RagClient  # noqa: E402
from quant_agent.orchestration.service import ApplicationService  # noqa: E402
from quant_agent.strategies.llm_fundamental import LlmFundamentalStrategy  # noqa: E402


def equity_of(account, market) -> Decimal:
    """当日市值权益 = cash + Σ(持仓 × 当日收盘)。"""
    price = {b.symbol: b.close for b in market.bars}
    holdings = Decimal("0")
    for p in account.positions:
        holdings += p.quantity * price.get(p.symbol, p.market_price)
    return account.cash + holdings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--symbols", nargs="+",
                    default=["AAPL", "MSFT", "GOOG", "AMZN", "META", "NVDA"])
    args = ap.parse_args()

    cfg = LlmConfig(api_url="https://api.deepseek.com", model="deepseek-v4-flash")
    client = LlmClient(cfg)
    if not client.has_key():
        print("FAIL: LLM key 未配置")
        return 1

    paths = ProjectPaths(ROOT)
    # 每次运行重置：删除上次模拟留下的市场快照（否则时钟从今天起，
    # 快照却已推进到未来 → DATA_TIME_IN_FUTURE）
    (paths.data_dir / "market_snapshot.json").unlink(missing_ok=True)
    # 共享冻结时钟：provider 行情与评估时钟同步快进（否则 DATA_TIME_IN_FUTURE）
    clock = FrozenClock(datetime.now(UTC).replace(microsecond=0))
    service = ApplicationService(paths=paths, clock=clock)
    provider = SimulatedMarketProvider(paths.data_dir, symbols=tuple(args.symbols),
                                       now_fn=clock.now)
    service.provider = provider
    strat = LlmFundamentalStrategy(cfg, client,
                                   RagClient(cfg.rag_url, cfg.rag_collection),
                                   audit=service.audit, clock=clock)
    service.strategy = strat
    service.seed_account(reset_runtime=True)

    initial = None
    equity_curve: list[tuple[str, Decimal]] = []
    trades = 0
    filled = 0
    t0 = time.perf_counter()

    print(f"模拟 {args.days} 天 · symbols={args.symbols} · 每日 LLM 信号\n")
    from dataclasses import replace as _replace
    for day in range(args.days):
        clock.advance(seconds=86400)      # 时钟 + 市场同步快进一天
        market = provider.load_market()
        # 未交易日也要刷新账户 as_of（否则 2 天后 ACCOUNT_STALE）
        account = service.provider.load_account()
        provider.save_account(_replace(account, as_of=clock.now()))
        account = service.provider.load_account()
        eq = equity_of(account, market)
        if initial is None:
            initial = eq
        equity_curve.append((market.as_of.date().isoformat(), eq))

        try:
            report = service.generate_report(None, request_id=f"sim-{day}")
            if report.plan.risk_decision.allowed_order_ids:
                service.approve_all(report.report_id, "sim-user")
                exec_result = service.execute(report.report_id, mode="paper",
                                              request_id=f"sim-exec-{day}")
                status = exec_result.get("execution_status", "?") \
                    if isinstance(exec_result, dict) else getattr(exec_result, "status", "?")
                if "FILLED" in str(status):
                    filled += 1
                trades += len(report.plan.orders)
        except Exception as exc:
            print(f"  第{day+1}天异常: {type(exc).__name__} {str(exc)[:80]}")

    final_market = provider.load_market()
    final_eq = equity_of(service.provider.load_account(), final_market)

    # ---- 报告 ----
    print("=" * 56)
    print(f"  模拟天数:        {args.days}")
    print(f"  初始权益:        {initial}")
    print(f"  最终权益:        {final_eq}")
    pnl = final_eq - initial
    ret = pnl / initial * 100 if initial else Decimal("0")
    print(f"  总收益:          {pnl:+}  ({ret:+.2f}%)")
    print(f"  下达订单:        {trades} 笔 · 成交 {filled} 次")
    print(f"  耗时:            {time.perf_counter()-t0:.1f}s")
    print("-" * 56)
    # 每日权益曲线（首末 + 极值标注）
    for date_s, eq in equity_curve:
        bar = "█" * max(1, int((eq / initial) * 20))
        print(f"  {date_s}  {eq:>12}  {bar}")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
