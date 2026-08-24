"""A 股日线多数据源拉取（新浪 / 腾讯 / 同花顺 / 东财 / 雪球）。

返回统一列：date, open, high, low, close, volume, source。
按配置顺序依次尝试，首个非空结果即返回。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

log = logging.getLogger("quant-agent")

DEFAULT_CN_SOURCES: tuple[str, ...] = ("gtimg", "ths", "sina", "tx", "em", "xq")
_CN_LOOKBACK_DAYS = 120
_THS_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.10jqka.com.cn/"}
_GTIMG_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
_XQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://xueqiu.com/"}


@dataclass(frozen=True)
class CnDailyFrame:
    """统一 A 股日线结果（轻量，不依赖 pandas）。"""

    source: str
    rows: tuple[dict[str, Any], ...]


def normalize_cn_sources(raw: str | tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_CN_SOURCES
    if isinstance(raw, str):
        items = [part.strip().lower() for part in raw.split(",") if part.strip()]
        return tuple(items) if items else DEFAULT_CN_SOURCES
    items = [str(part).strip().lower() for part in raw if str(part).strip()]
    return tuple(items) if items else DEFAULT_CN_SOURCES


def _get_fetcher(name: str) -> Callable[..., CnDailyFrame] | None:
    alias = {"xueqiu": "xq"}
    key = alias.get(name, name)
    fn = globals().get(f"_fetch_{key}")
    return fn if callable(fn) else None


def fetch_cn_daily(
    code: str,
    *,
    sources: tuple[str, ...] | None = None,
    lookback: int = _CN_LOOKBACK_DAYS,
    xq_token: str | None = None,
) -> CnDailyFrame:
    """按顺序尝试各数据源，返回首个成功结果。"""
    code = code.zfill(6)
    chain = sources or DEFAULT_CN_SOURCES
    errors: list[str] = []
    for name in chain:
        fetcher = _get_fetcher(name)
        if fetcher is None:
            errors.append(f"{name}: unknown source")
            continue
        try:
            frame = fetcher(code, lookback=lookback, xq_token=xq_token)
            if frame.rows:
                return frame
            errors.append(f"{name}: empty")
        except Exception as exc:  # noqa: BLE001 — 尝试下一数据源
            errors.append(f"{name}: {exc}")
            log.debug("A股日线 %s 拉取失败 %s: %s", code, name, exc)
    joined = "；".join(errors) if errors else "no sources configured"
    raise ValueError(f"所有 A 股数据源失败（{joined}）")


def _market_prefix(code: str) -> tuple[str, str]:
    """返回 (sina/tx 前缀, 同花顺 market)。"""
    if code.startswith(("5", "6", "9")):
        return "sh", "hs"
    return "sz", "sz"


def _parse_yyyymmdd(raw: str) -> str:
    text = str(raw).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _rows_to_frame(source: str, rows: list[dict[str, Any]]) -> CnDailyFrame:
    dedup: dict[str, dict[str, Any]] = {}
    for row in rows:
        dedup[str(row["date"])] = row
    ordered = tuple(dedup[key] for key in sorted(dedup))
    return CnDailyFrame(source=source, rows=ordered)


def _tail_rows(frame: CnDailyFrame, lookback: int) -> CnDailyFrame:
    if len(frame.rows) <= lookback:
        return frame
    return CnDailyFrame(source=frame.source, rows=frame.rows[-lookback:])


def _fetch_sina(code: str, *, lookback: int, xq_token: str | None) -> CnDailyFrame:
    import akshare as ak

    prefix, _ = _market_prefix(code)
    df = ak.stock_zh_a_daily(symbol=prefix + code, adjust="qfq")
    if df is None or getattr(df, "empty", True):
        return CnDailyFrame(source="sina", rows=())
    rows = [
        {
            "date": str(row["date"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "source": "sina",
        }
        for _, row in df.tail(lookback).iterrows()
    ]
    return _rows_to_frame("sina", rows)


def _fetch_tx(code: str, *, lookback: int, xq_token: str | None) -> CnDailyFrame:
    import akshare as ak

    prefix, _ = _market_prefix(code)
    df = ak.stock_zh_a_hist_tx(symbol=prefix + code)
    if df is None or getattr(df, "empty", True):
        return CnDailyFrame(source="tx", rows=())
    rows = [
        {
            "date": str(row["date"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
            "source": "tx",
        }
        for _, row in df.tail(lookback).iterrows()
    ]
    return _rows_to_frame("tx", rows)


def _fetch_gtimg(code: str, *, lookback: int, xq_token: str | None) -> CnDailyFrame:
    import requests

    prefix, _ = _market_prefix(code)
    symbol = f"{prefix}{code}"
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,,,{lookback},qfq"
    )
    resp = requests.get(url, headers=_GTIMG_HEADERS, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    series = payload["data"][symbol]["qfqday"]
    rows = [
        {
            "date": str(item[0]),
            "open": float(item[1]),
            "close": float(item[2]),
            "high": float(item[3]),
            "low": float(item[4]),
            "volume": float(item[5]),
            "source": "gtimg",
        }
        for item in series
        if item and len(item) >= 6
    ]
    normalized = [
        {
            "date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "source": "gtimg",
        }
        for row in rows
    ]
    return _tail_rows(_rows_to_frame("gtimg", normalized), lookback)


def _fetch_ths(code: str, *, lookback: int, xq_token: str | None) -> CnDailyFrame:
    import requests

    _, market = _market_prefix(code)
    years = {datetime.now(UTC).year, datetime.now(UTC).year - 1}
    rows: list[dict[str, Any]] = []
    for year in sorted(years):
        url = f"https://d.10jqka.com.cn/v6/line/{market}_{code}/01/{year}.js"
        resp = requests.get(url, headers=_THS_HEADERS, timeout=20)
        resp.raise_for_status()
        match = re.search(r"quotebridge_v6_line_[^(]+\((.*)\)\s*$", resp.text, re.S)
        if not match:
            continue
        payload = json.loads(match.group(1))
        for chunk in str(payload.get("data", "")).split(";"):
            if not chunk:
                continue
            parts = chunk.split(",")
            if len(parts) < 6:
                continue
            rows.append(
                {
                    "date": _parse_yyyymmdd(parts[0]),
                    "open": float(parts[1]),
                    "high": float(parts[2]),
                    "low": float(parts[3]),
                    "close": float(parts[4]),
                    "volume": float(parts[5]),
                    "source": "ths",
                }
            )
    return _tail_rows(_rows_to_frame("ths", rows), lookback)


def _fetch_em(code: str, *, lookback: int, xq_token: str | None) -> CnDailyFrame:
    import akshare as ak

    df = ak.stock_zh_a_hist(
        symbol=code, period="daily", start_date="", end_date="", adjust="qfq"
    )
    if df is None or getattr(df, "empty", True):
        return CnDailyFrame(source="em", rows=())
    rows = [
        {
            "date": _parse_yyyymmdd(row["日期"]),
            "open": float(row["开盘"]),
            "high": float(row["最高"]),
            "low": float(row["最低"]),
            "close": float(row["收盘"]),
            "volume": float(row["成交量"]),
            "source": "em",
        }
        for _, row in df.tail(lookback).iterrows()
    ]
    return _rows_to_frame("em", rows)


def _resolve_xq_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for key in ("QUANT_AGENT_XQ_TOKEN", "XQ_A_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _fetch_xq(code: str, *, lookback: int, xq_token: str | None) -> CnDailyFrame:
    token = _resolve_xq_token(xq_token)
    if not token:
        raise ValueError("雪球需配置 QUANT_AGENT_XQ_TOKEN 或 XQ_A_TOKEN")
    import requests

    prefix, _ = _market_prefix(code)
    symbol = f"{prefix.upper()}{code}"
    url = (
        "https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={symbol}&begin=0&period=day&type=before&count=-{lookback}&indicator=kline"
    )
    session = requests.Session()
    session.headers.update(_XQ_HEADERS)
    session.cookies.set("xq_a_token", token, domain=".xueqiu.com")
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error_code"):
        raise ValueError(str(payload.get("error_description") or payload.get("error_code")))
    items = payload.get("data", {}).get("item") or []
    columns = payload.get("data", {}).get("column") or []
    idx = {name: pos for pos, name in enumerate(columns)}
    required = ("timestamp", "open", "high", "low", "close", "volume")
    if not all(key in idx for key in required):
        raise ValueError("雪球 kline 列结构异常")
    rows: list[dict[str, Any]] = []
    for item in items:
        ts_ms = int(item[idx["timestamp"]])
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y-%m-%d")
        rows.append(
            {
                "date": dt,
                "open": float(item[idx["open"]]),
                "high": float(item[idx["high"]]),
                "low": float(item[idx["low"]]),
                "close": float(item[idx["close"]]),
                "volume": float(item[idx["volume"]]),
                "source": "xq",
            }
        )
    return _tail_rows(_rows_to_frame("xq", rows), lookback)
