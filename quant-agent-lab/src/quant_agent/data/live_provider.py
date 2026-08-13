"""LiveMarketDataProvider：自动拉取行情（美股 yfinance / A股 akshare）。

- 依赖 lazy import：未安装对应库时才报错（模块导入不受影响）
- 拉取成功落盘缓存 var/data/market_snapshot.json（与 FileDataProvider 同格式，
  拉取失败回退上次缓存——离线可用）
- symbol 约定：纯数字（600519 等）= A股（akshare）；其余 = 美股（yfinance）
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from quant_agent.data.providers import FileDataProvider
from quant_agent.domain.models import MarketBar, MarketSnapshot

log = logging.getLogger("quant-agent")

_LOOKBACK_DAYS = "3mo"     # yfinance period
_CN_LOOKBACK = "90"        # akshare 默认近 90 交易日


def _is_cn_symbol(symbol: str) -> bool:
    return symbol.isdigit()


def _ensure_curl_ca() -> None:
    """curl_cffi 无法加载含非 ASCII 字符路径的 CAfile（项目在中文路径下时
    certifi 路径含中文 → SSL trust anchor 错误）。复制 CA 到纯 ASCII 路径并
    设置 CURL_CA_BUNDLE（幂等：已存在则复用）。"""
    import os
    if os.environ.get("CURL_CA_BUNDLE"):
        return
    try:
        import certifi
        ca_src = certifi.where()
        if all(ord(c) < 128 for c in ca_src):
            os.environ["CURL_CA_BUNDLE"] = ca_src
            return
        ascii_ca = os.path.join(os.path.expanduser("~"), "cacert.pem")
        if not os.path.exists(ascii_ca):
            import shutil
            shutil.copy(ca_src, ascii_ca)
        os.environ["CURL_CA_BUNDLE"] = ascii_ca
    except Exception:
        pass    # 证书设置失败让 yfinance 自行报错（保持降级语义）


class MarketDataUnavailable(Exception):
    """行情拉取失败且无缓存可回退。"""


class LiveMarketDataProvider(FileDataProvider):
    """自动拉取行情（美股 yfinance / A股 akshare），继承 FileDataProvider：

    - 缓存复用 market_path（拉取成功落盘；失败回退上次缓存——离线可用）
    - load_account 等文件读取保留（账户仍来自本地 JSON）
    """

    def __init__(self, data_dir: Path, market: str = "us",
                 symbols: tuple[str, ...] = ()):
        super().__init__(data_dir)
        self.market = market        # us | cn | both
        self.symbols = symbols

    def load_market(self) -> MarketSnapshot:
        symbols = list(self.symbols)
        if not symbols:
            raise MarketDataUnavailable("未配置行情 symbols（config.market_data.symbols）")
        bars: list[MarketBar] = []
        errors: list[str] = []
        for symbol in symbols:
            try:
                bars.extend(self._fetch(symbol))
            except Exception as exc:      # 单个 symbol 失败不阻塞其余
                errors.append(f"{symbol}: {exc}")
                log.warning("行情拉取失败 %s：%s", symbol, exc)
        if not bars:
            raise MarketDataUnavailable(
                "所有行情拉取失败（" + "；".join(errors) + "）")
        snapshot = MarketSnapshot(
            snapshot_id=f"live-{uuid.uuid4().hex[:8]}",
            as_of=max(b.timestamp for b in bars),
            source="live-yfinance/akshare",
            bars=tuple(bars),
        )
        self.save_market(snapshot)      # 缓存（继承自 FileDataProvider）
        return snapshot

    def load_market_or_cache(self) -> MarketSnapshot:
        """拉取失败回退上次缓存（离线可用）。"""
        try:
            return self.load_market()
        except MarketDataUnavailable:
            if self.market_path.exists():
                log.warning("行情拉取失败，回退缓存 %s", self.market_path)
                from quant_agent.domain.codec import market_snapshot_from_dict
                import json
                return market_snapshot_from_dict(
                    json.loads(self.market_path.read_text(encoding="utf-8")))
            raise

    # ---- 缓存 ----
    # save_market / market_path 继承自 FileDataProvider（拉取成功时已调用）

    def _fetch(self, symbol: str) -> list[MarketBar]:
        if _is_cn_symbol(symbol):
            if self.market not in ("cn", "both"):
                raise ValueError(f"symbol {symbol} 是 A股，但 market={self.market}")
            return self._fetch_akshare(symbol)
        if self.market not in ("us", "both"):
            raise ValueError(f"symbol {symbol} 是美股，但 market={self.market}")
        return self._fetch_yfinance(symbol)

    def _fetch_yfinance(self, symbol: str) -> list[MarketBar]:
        _ensure_curl_ca()        # 中文路径 CA 修复（幂等）
        import yfinance as yf     # lazy：未安装才报错
        df = yf.download(symbol, period=_LOOKBACK_DAYS, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            raise ValueError("yfinance 返回空数据")
        bars = []
        for ts, row in df.iterrows():
            dt = _to_utc(ts)
            bars.append(MarketBar(
                symbol=symbol, timestamp=dt,
                open=_dec(row["Open"]), high=_dec(row["High"]),
                low=_dec(row["Low"]), close=_dec(row["Close"]),
                volume=_dec(row["Volume"]), currency="USD",
                timeframe="1d", source="yfinance", is_synthetic=False,
                session="regular", snapshot_id="",
            ))
        return bars

    def _fetch_akshare(self, symbol: str) -> list[MarketBar]:
        import akshare as ak     # lazy：未安装才报错
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                start_date="", end_date="", adjust="qfq")
        if df is None or df.empty:
            raise ValueError("akshare 返回空数据")
        bars = []
        for _, row in df.tail(90).iterrows():
            dt = datetime.strptime(str(row["日期"]), "%Y-%m-%d").replace(tzinfo=UTC)
            bars.append(MarketBar(
                symbol=symbol, timestamp=dt,
                open=_dec(row["开盘"]), high=_dec(row["最高"]),
                low=_dec(row["最低"]), close=_dec(row["收盘"]),
                volume=_dec(row["成交量"]), currency="CNY",
                timeframe="1d", source="akshare", is_synthetic=False,
                session="regular", snapshot_id="",
            ))
        return bars


def _dec(value) -> Decimal:
    d = Decimal(str(value))
    return d.quantize(Decimal("0.0001")) if d == d else Decimal("0")


def _to_utc(ts) -> datetime:
    """pandas Timestamp / datetime / ISO 字符串 → 时区感知 datetime（UTC）。"""
    try:
        import pandas as pd     # noqa: F401  — 仅类型判断用；未装不影响 datetime 路径
        if isinstance(ts, pd.Timestamp):
            if ts.tzinfo is None:
                return ts.tz_localize("UTC").to_pydatetime()
            return ts.tz_convert("UTC").to_pydatetime()
    except ImportError:
        pass
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
