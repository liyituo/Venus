"""LlmFundamentalStrategy 测试：信号结构/校验/降级/防未来函数/审计无密钥。

全部假 LlmClient / 假 RagClient（不触网）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal


from quant_agent.domain.enums import SignalDirection
from quant_agent.domain.models import MarketBar, MarketSnapshot
from quant_agent.llm.client import LlmConfig, LlmUnavailable
from quant_agent.strategies.llm_fundamental import (
    LlmFundamentalStrategy,
    _parse_llm_json,
    _reports_context,
)


def _bar(symbol: str, day: int, close: str) -> MarketBar:
    day_dt = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=day)
    return MarketBar(
        symbol=symbol, timestamp=day_dt,
        open=Decimal(close), high=Decimal(close), low=Decimal(close),
        close=Decimal(close), volume=Decimal("1000"),
        source="test-fixture", is_synthetic=True, snapshot_id="s1",
    )


def _snapshot(bars: list[MarketBar]) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id="s1", as_of=bars[-1].timestamp, source="test-fixture",
        bars=tuple(bars),
    )


class FakeLlm:
    def __init__(self, response: str, fail: bool = False):
        self.response = response
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, **kw) -> str:
        if self.fail:
            raise LlmUnavailable("fake failure")
        self.calls.append((system, user))
        return self.response


class FakeRag:
    def __init__(self, hits: list[dict]):
        self.hits = hits
        self.queries: list[str] = []

    def search(self, query, **kw):
        self.queries.append(query)
        return self.hits

    def available(self) -> bool:
        return True


class FakeAudit:
    def __init__(self):
        self.events: list[dict] = []

    def record(self, event_type, **kw):
        self.events.append({"event_type": event_type, **kw})


CFG = LlmConfig(api_url="http://fake", model="m", api_key_env="FAKE_KEY")


def _make_strategy(llm, rag=None, audit=None):
    return LlmFundamentalStrategy(CFG, llm, rag or FakeRag([]), audit=audit)


# ============ 1. 正常信号 ============
def test_buy_signal_structure():
    llm = FakeLlm(json.dumps({
        "direction": "BUY", "strength": 0.8, "reason_code": "营收超预期",
        "invalidation_conditions": ["跌破支撑位"],
    }))
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    strat = _make_strategy(llm, FakeRag([
        {"doc_id": "d1", "title": "AAPL 财报", "text": "营收增长 20%",
         "score": 0.9, "meta": {"symbol": "AAPL", "report_date": "2026-08-01"}},
    ]))
    signals = strat.generate(_snapshot(bars))
    assert len(signals) == 1
    s = signals[0]
    assert s.direction == SignalDirection.BUY
    assert s.strength == Decimal("0.8")
    assert s.reason_code == "营收超预期"
    assert s.strategy_id == "llm-fundamental"
    assert s.reference_price == Decimal("100")
    # prompt 包含财报与行情
    prompt = llm.calls[0][1]
    assert "AAPL 财报" in prompt and "营收增长 20%" in prompt
    assert "O=" in prompt    # 行情上下文


# ============ 2. 降级三态 ============
def test_llm_unavailable_degrades_to_hold():
    strat = _make_strategy(FakeLlm("", fail=True))
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    s = strat.generate(_snapshot(bars))[0]
    assert s.direction == SignalDirection.HOLD
    assert s.reason_code == "LLM_UNAVAILABLE"
    assert s.strength == Decimal("0")


def test_bad_json_degrades_to_hold():
    strat = _make_strategy(FakeLlm("这不是 JSON"))
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    s = strat.generate(_snapshot(bars))[0]
    assert s.direction == SignalDirection.HOLD
    assert s.reason_code == "LLM_BAD_OUTPUT"


def test_invalid_direction_degrades():
    strat = _make_strategy(FakeLlm(json.dumps({"direction": "LONG"})))
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    s = strat.generate(_snapshot(bars))[0]
    assert s.direction == SignalDirection.HOLD
    assert s.reason_code == "LLM_BAD_OUTPUT"


def test_not_configured_degrades():
    strat = LlmFundamentalStrategy(LlmConfig(), FakeLlm("x"), FakeRag([]))
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    s = strat.generate(_snapshot(bars))[0]
    assert s.reason_code == "LLM_NOT_CONFIGURED"


# ============ 3. markdown 围栏解析 ============
def test_fenced_json_parsed():
    out = _parse_llm_json('```json\n{"direction": "SELL"}\n```')
    assert out and out["direction"] == "SELL"
    assert _parse_llm_json("garbage") is None


# ============ 4. 防未来函数 ============
def test_future_report_excluded():
    hits = [
        {"title": "未来财报", "text": "Q3 预测",
         "meta": {"report_date": "2026-09-15"}},    # 晚于 as_of(8月10日)
        {"title": "当期财报", "text": "Q2 营收",
         "meta": {"report_date": "2026-08-05"}},
    ]
    as_of = datetime(2026, 8, 10, tzinfo=UTC)
    ctx = _reports_context(hits, as_of)
    assert "当期财报" in ctx
    assert "未来财报" not in ctx


# ============ 5. 审计（无密钥）===========
def test_audit_events_recorded_without_key():
    audit = FakeAudit()
    strat = _make_strategy(FakeLlm(json.dumps({"direction": "BUY", "strength": 0.5,
                                               "reason_code": "OK",
                                               "invalidation_conditions": ["x"]})),
                           FakeRag([{"title": "r", "text": "t", "score": 1,
                                     "meta": {"symbol": "AAPL"}}]),
                           audit=audit)
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    strat.generate(_snapshot(bars))
    assert audit.events, "审计事件未记录"
    ev = audit.events[0]
    assert ev["event_type"] == "llm_signal.generated"
    assert ev["reason_code"] == "BUY"
    assert "sk-" not in ev["input_summary"]
    assert "AAPL" in ev["input_summary"]


# ============ 6. RAG 不可达：仅行情 prompt ============
def test_rag_unavailable_prompt_has_no_reports():
    class DownRag:
        def search(self, query, **kw):
            return []
        def available(self):
            return False

    strat = _make_strategy(FakeLlm(json.dumps({"direction": "HOLD", "strength": 0,
                                               "reason_code": "NO_EVIDENCE",
                                               "invalidation_conditions": ["x"]})),
                           DownRag())
    bars = [_bar("AAPL", i, "100") for i in range(10)]
    # 断言 RAG 不可达时 prompt 不含财报片段
    llm = FakeLlm(json.dumps({"direction": "HOLD", "strength": 0,
                              "reason_code": "NO_EVIDENCE",
                              "invalidation_conditions": ["x"]}))
    strat = _make_strategy(llm, DownRag())
    strat.generate(_snapshot(bars))
    prompt = llm.calls[0][1]
    assert "财报要点" not in prompt and "无财报上下文" in prompt
    s = _make_strategy(FakeLlm(json.dumps({"direction": "HOLD", "strength": 0,
                                           "reason_code": "NO_EVIDENCE",
                                           "invalidation_conditions": ["x"]})),
                       DownRag()).generate(_snapshot(bars))[0]
    assert s.direction == SignalDirection.HOLD


# ============ 7. 数据不足 ============
def test_insufficient_data():
    strat = _make_strategy(FakeLlm("x"))
    s = strat.generate(_snapshot([_bar("AAPL", 0, "100")]))[0]
    assert s.reason_code == "INSUFFICIENT_DATA"
