"""LlmFundamentalStrategy：LLM 基本面信号策略。

流程（每个 symbol）：
1. 行情上下文：最近 20 日 OHLCV 摘要（价格趋势/波动）
2. 财报检索：RagClient 按 symbol 检索财报要点（top_k 片段）
3. DeepSeek 结构化输出：BUY/SELL/HOLD + reason_code + strength + invalidation
4. 校验 → StrategySignal（下游风险/审批/执行完全复用现有链路）
5. 失败降级：LLM 不可达/输出非法 → HOLD + LLM_UNAVAILABLE（绝不伪造信号）

防未来函数：检索只用 report_date ≤ market.as_of 的财报片段。
审计：每次 LLM 调用写 AuditEvent（prompt/响应摘要，不含 API key）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from quant_agent.domain.enums import SignalDirection
from quant_agent.domain.models import MarketBar, MarketSnapshot, StrategySignal
from quant_agent.llm.client import LlmClient, LlmConfig, LlmUnavailable
from quant_agent.llm.rag_client import RagClient

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_MAX_REASON = 40
_PRICE_LOOKBACK = 20
_MAX_STRENGTH = Decimal("1.0")

_SYSTEM_PROMPT = (
    "你是量化基本面分析师。根据给定的行情数据与财报要点，输出对一个股票的"
    "日级交易信号。\n"
    "严格只输出一个 JSON 对象（不要 markdown、不要解释），字段：\n"
    '{"direction": "BUY|SELL|HOLD", "strength": 0到1之间的数字, '
    '"reason_code": "简短理由码(≤40字符，必须能追溯到输入证据)", '
    '"invalidation_conditions": ["信号失效条件1", "条件2"]}\n'
    "规则：理由必须引用输入中的具体数据（价格/营收/趋势）；"
    "证据不足或矛盾时输出 HOLD；不得臆测输入之外的信息。"
)


def _market_context(bars: list[MarketBar], lookback: int = _PRICE_LOOKBACK) -> str:
    window = bars[-lookback:]
    lines = [f"行情（{bars[0].timestamp.date() if bars else '?'} → "
             f"{bars[-1].timestamp.date() if bars else '?'}，共 {len(window)} 日）："]
    for b in window:
        lines.append(f"{b.timestamp.date()} O={b.open} H={b.high} L={b.low} "
                     f"C={b.close} V={b.volume}")
    return "\n".join(lines)


def _reports_context(hits: list[dict], as_of: datetime) -> str:
    """财报片段；防未来函数：report_date 晚于 as_of 的丢弃。"""
    kept = []
    for h in hits:
        rd = ((h.get("meta") or {}).get("report_date") or "")
        if rd:
            try:
                report_dt = datetime.fromisoformat(str(rd)[:10])
                if report_dt.date() > as_of.date():
                    continue    # 未来财报不得进入 prompt
            except ValueError:
                pass
        kept.append(h)
    if not kept:
        return ""
    lines = ["财报要点（RAG 检索）："]
    for h in kept[:5]:
        lines.append(f"- [{h.get('title') or '无标题'}] {h.get('text', '')[:400]}")
    return "\n".join(lines)


def _parse_llm_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    m = _FENCE_RE.search(raw)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


class LlmFundamentalStrategy:
    strategy_id = "llm-fundamental"
    version = "1.0.0"

    def __init__(self, llm_config: LlmConfig, llm: LlmClient, rag: RagClient,
                 audit=None, clock=None, version: str = "1.0.0"):
        self.config = llm_config
        self.llm = llm
        self.rag = rag
        self.audit = audit          # AuditLogger | None
        self.clock = clock
        self.version = version

    def generate(self, market: MarketSnapshot) -> tuple[StrategySignal, ...]:
        signals: list[StrategySignal] = []
        for symbol, bars in sorted(market.bars_by_symbol().items()):
            signals.append(self._signal_for(symbol, bars, market))
        return tuple(signals)

    # ---- 单 symbol 信号 ----
    def _signal_for(self, symbol: str, bars: tuple[MarketBar, ...],
                    market: MarketSnapshot) -> StrategySignal:
        end = market.as_of
        start = bars[0].timestamp if bars else end
        ref_price = bars[-1].close if bars else Decimal("0")

        def _hold(reason: str, extra: str = "") -> StrategySignal:
            return StrategySignal(
                symbol=symbol, direction=SignalDirection.HOLD,
                strength=Decimal("0"), reason_code=reason,
                input_start=start, input_end=end,
                strategy_id=self.strategy_id, strategy_version=self.version,
                invalidation_conditions=("新的有效行情或财报改变判断",),
                reference_price=ref_price)

        if len(bars) < 2 or ref_price <= 0:
            return _hold("INSUFFICIENT_DATA")
        if not self.config.enabled:
            return _hold("LLM_NOT_CONFIGURED")

        market_ctx = _market_context(list(bars))
        reports = self.rag.search(f"{symbol} 财报 营收 利润 基本面", top_k=5,
                                  symbol=symbol)
        reports_ctx = _reports_context(reports, end)
        user = f"股票：{symbol}\n\n{market_ctx}\n\n{reports_ctx or '（无财报上下文：仅基于行情判断）'}"
        try:
            raw = self.llm.complete(_SYSTEM_PROMPT, user)
        except LlmUnavailable as exc:
            if self.audit is not None:
                self._audit(symbol, user, f"LLM_UNAVAILABLE: {exc}", "HOLD")
            return _hold("LLM_UNAVAILABLE")

        parsed = _parse_llm_json(raw)
        if parsed is None:
            if self.audit is not None:
                self._audit(symbol, user, f"非法输出: {raw[:200]}", "HOLD")
            return _hold("LLM_BAD_OUTPUT")

        direction_raw = str(parsed.get("direction") or "").upper()
        try:
            direction = SignalDirection(direction_raw)
        except ValueError:
            if self.audit is not None:
                self._audit(symbol, user, f"非法方向: {direction_raw}", "HOLD")
            return _hold("LLM_BAD_OUTPUT")
        strength = _safe_strength(parsed.get("strength"))
        reason = str(parsed.get("reason_code") or "LLM_SIGNAL")[:_MAX_REASON]
        invalidation = tuple(
            str(c)[:120] for c in (parsed.get("invalidation_conditions") or [])
        ) or ("新的有效行情或财报改变判断",)
        if self.audit is not None:
            self._audit(symbol, user, f"{direction.value} {reason}", direction.value)
        return StrategySignal(
            symbol=symbol, direction=direction, strength=strength,
            reason_code=reason, input_start=start, input_end=end,
            strategy_id=self.strategy_id, strategy_version=self.version,
            invalidation_conditions=invalidation, reference_price=ref_price)

    def _audit(self, symbol: str, prompt: str, result: str, reason: str) -> None:
        """LLM 决策审计（不含 API key；摘要截断 500 字符）。"""
        try:
            self.audit.record(
                "llm_signal.generated", actor="llm-fundamental",
                reason_code=reason,
                input_summary=f"{symbol}: {prompt[:500]}",
                result_summary=result[:500])
        except Exception:
            pass    # 审计失败不影响信号生成


def _safe_strength(value) -> Decimal:
    try:
        s = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    if s <= 0 or s > _MAX_STRENGTH:
        return Decimal("0")
    return s.quantize(Decimal("0.0001"))
