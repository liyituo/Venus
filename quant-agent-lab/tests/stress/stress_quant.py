"""量化中心压力测试：LLM 信号稳定性 / 并发 / 模拟市场多日推进 / 全链路循环。

真实 LLM（DeepSeek flash，费用极低）+ 模拟市场（无网络）。
输出汇总报告；exit code 非 0 表示有硬失败。
"""

from __future__ import annotations

import concurrent.futures
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_agent.data.simulated_provider import SimulatedMarketProvider  # noqa: E402
from quant_agent.infrastructure.config import LlmConfig  # noqa: E402
from quant_agent.llm.client import LlmClient  # noqa: E402
from quant_agent.llm.rag_client import RagClient  # noqa: E402
from quant_agent.strategies.llm_fundamental import (  # noqa: E402
    LlmFundamentalStrategy,
    _parse_llm_json,
)

CFG = LlmConfig(api_url="https://api.deepseek.com", model="deepseek-v4-flash")
RAG_URL = "http://127.0.0.1:8010"

_SYS = (
    '严格只输出一个 JSON 对象：{"direction":"BUY|SELL|HOLD","strength":0到1,'
    '"reason_code":"≤40字符","invalidation_conditions":["条件"]}。不要markdown不要解释。'
)
_USER_TMPL = "股票：{sym}\n\n行情：近5日收盘 100,101,103,102,104\n\n财报：营收增长8%"


def _signal_once(client: LlmClient, i: int) -> dict:
    t0 = time.perf_counter()
    try:
        raw = client.complete(
            _SYS, _USER_TMPL.format(sym=f"T{i}"), max_tokens=400, thinking_disabled=True
        )
        ok = _parse_llm_json(raw) is not None
        return {
            "ok": ok,
            "latency": time.perf_counter() - t0,
            "reason": "OK" if ok else "BAD_OUTPUT",
        }
    except Exception as exc:
        return {"ok": False, "latency": time.perf_counter() - t0, "reason": type(exc).__name__}


def main() -> int:
    client = LlmClient(CFG)
    if not client.has_key():
        print("FAIL: LLM key 未配置（.env）")
        return 1
    print(f"key: {client._key[:6]}… | model: {CFG.model} | URL: {client._url}\n")

    # ============ Phase 1：连续调用稳定性（30 次）============
    print("== Phase 1: 连续 30 次 LLM 调用 ==")
    results = [_signal_once(client, i) for i in range(30)]
    ok = [r for r in results if r["ok"]]
    lat = sorted(r["latency"] for r in results)
    reasons = {}
    for r in results:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    print(f"  成功率: {len(ok)}/30 ({len(ok) / 30 * 100:.0f}%)")
    print(f"  延迟: p50={lat[15]:.2f}s p95={lat[28]:.2f}s max={lat[-1]:.2f}s")
    print(f"  失败分布: {reasons}")

    # ============ Phase 2：并发 20 路 LLM ============
    print("\n== Phase 2: 并发 20 路 LLM 调用 ==")
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        conc = list(pool.map(lambda i: _signal_once(client, 100 + i), range(20)))
    conc_ok = sum(1 for r in conc if r["ok"])
    print(
        f"  并发成功率: {conc_ok}/20 | 总耗时: {time.perf_counter() - t0:.2f}s "
        f"（串行≈{sum(r['latency'] for r in conc):.0f}s，并行加速≈"
        f"{sum(r['latency'] for r in conc) / max(0.01, time.perf_counter() - t0):.1f}x）"
    )

    # ============ Phase 3：模拟市场 30 天推进 ============
    print("\n== Phase 3: 模拟市场 30 天推进（快进时钟）==")
    tmp = Path("var/stress-market")
    tmp.mkdir(parents=True, exist_ok=True)
    # 可注入时钟：从今天开始每天快进一天
    day_holder = {"now": datetime.now(UTC)}

    def fake_now():
        return day_holder["now"]

    provider = SimulatedMarketProvider(tmp, symbols=("AAPL", "MSFT"), now_fn=fake_now)
    snap = provider.load_market()
    first_close = snap.bars[-1].close
    # 快进 30 天（模拟每天调用）
    errors = 0
    for day in range(1, 31):
        day_holder["now"] = day_holder["now"] + timedelta(days=1)
        try:
            provider.load_market()
        except Exception as exc:
            errors += 1
            print(f"    第{day}天异常: {exc}")
    final = provider.load_market()
    print(f"  30 天推进异常数: {errors} | 起始价 {first_close} → 30天后 {final.bars[-1].close}")
    print(f"  总 bar 数: {len(final.bars)}（初始 82 → 应约 142）")

    # ============ Phase 4：全链路循环 5 次（真实 demo）============
    print("\n== Phase 4: 全链路循环 5 次（行情→LLM→风控→纸面执行）==")
    from quant_agent.infrastructure.clock import SystemClock
    from quant_agent.infrastructure.paths import ProjectPaths
    from quant_agent.orchestration.service import ApplicationService

    service = ApplicationService(paths=ProjectPaths(ROOT), clock=SystemClock())
    strat = LlmFundamentalStrategy(
        CFG,
        client,
        RagClient(RAG_URL, "financial-reports"),
        audit=service.audit,
        clock=service.clock,
    )
    service.strategy = strat
    ok_runs = 0
    for i in range(5):
        t0 = time.perf_counter()
        try:
            report = service.generate_report(None, request_id=f"stress-{i}")
            status = report.status.value
            if report.plan.risk_decision.allowed_order_ids:
                service.approve_all(report.report_id, "stress-user")
                service.execute(report.report_id, mode="paper", request_id=f"stress-exec-{i}")
                ok_runs += 1
            else:
                # 无订单（全 HOLD 或风控拦截）也算链路成功（降级语义）
                ok_runs += 1
            print(
                f"  第{i + 1}次: {status} | {time.perf_counter() - t0:.2f}s | "
                f"信号{len(report.plan.signals)} 订单{len(report.plan.orders)}"
            )
        except Exception as exc:
            print(f"  第{i + 1}次失败: {type(exc).__name__} {str(exc)[:100]}")
    print(f"  全链路成功率: {ok_runs}/5")

    # ============ 汇总 ============
    print("\n===== 压力测试汇总 =====")
    seq_rate = len(ok) / 30
    conc_rate = conc_ok / 20
    hard_fail = seq_rate < 0.8 or conc_rate < 0.8 or ok_runs < 4 or errors > 0
    print(f"  连续调用成功率: {seq_rate * 100:.0f}% (门槛 80%)")
    print(f"  并发成功率: {conc_rate * 100:.0f}% (门槛 80%)")
    print(f"  全链路: {ok_runs}/5 (门槛 4)")
    print(f"  市场推进异常: {errors} (门槛 0)")
    print(f"  → {'FAIL' if hard_fail else 'PASS'}")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
