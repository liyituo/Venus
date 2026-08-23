"""fetch_csi300_akshare.py：从 akshare 抓取 CSI300 成分股日线（真实数据）。

数据源优先级: 新浪（stock_zh_a_daily, qfq）-> 腾讯（stock_zh_a_hist_tx）-> 东财（stock_zh_a_hist）。

注意:
    - 成分股为"当前"沪深300 成分（存在幸存者偏差，文档中已注明）；
    - 前复权价格（qfq）；
    - 生成 data/csi300_raw/{ohlcv.csv, benchmark.csv}，
      再交给 qlib_adapter 的 csv 模式构建统一格式。

用法:
    python -u scripts/fetch_csi300_akshare.py --out data/csi300_raw [--limit 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="akshare 抓取 CSI300 日线")
    p.add_argument("--out", default="data/csi300_raw")
    p.add_argument("--start", default="20140101")
    p.add_argument("--end", default="20241231")
    p.add_argument("--limit", type=int, default=None, help="只抓前 N 只（调试用）")
    p.add_argument("--workers", type=int, default=8)
    return p.parse_args()


def to_sina_code(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def to_tx_code(code: str) -> str:
    return ("sh" if code.startswith("6") else "sz") + code


def fetch_one(code: str, start: str, end: str) -> tuple[str, object]:
    """抓取单只股票日线（新浪 -> 腾讯 -> 东财），返回 (code, DataFrame 或 None)。"""
    import akshare as ak
    import pandas as pd

    last_err = None
    # 1) 新浪（主）
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(
                symbol=to_sina_code(code), start_date=start, end_date=end, adjust="qfq"
            )
            if df is not None and not df.empty:
                df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                                        "low": "low", "close": "close",
                                        "volume": "volume", "amount": "amount"})
                df["date"] = df["date"].astype(str)
                df["symbol"] = code
                return code, df[["date", "symbol", "open", "high", "low", "close",
                                 "volume", "amount"]]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.0 + attempt)
    # 2) 腾讯（备）
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist_tx(symbol=to_tx_code(code),
                                       start_date=start, end_date=end)
            if df is not None and not df.empty:
                df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                                        "low": "low", "close": "close", "volume": "volume"})
                df["date"] = df["date"].astype(str)
                df["symbol"] = code
                df["amount"] = 0.0
                return code, df[["date", "symbol", "open", "high", "low", "close",
                                 "volume", "amount"]]
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.0 + attempt)
    return code, last_err


def main() -> None:
    args = parse_args()
    import akshare as ak
    import pandas as pd

    os.makedirs(args.out, exist_ok=True)

    # 1) 当前 CSI300 成分股（优先使用缓存，csindex 接口偶发挂起）
    cache_path = os.path.join(args.out, "constituents.json")
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            symbols = json.load(f)
        print(f"使用缓存成分股: {len(symbols)} 只", flush=True)
    else:
        cons = ak.index_stock_cons_csindex(symbol="000300")
        code_col = "成分券代码" if "成分券代码" in cons.columns else "品种代码"
        symbols = cons[code_col].astype(str).str.zfill(6).tolist()
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(symbols, f)
        print(f"CSI300 成分股: {len(symbols)} 只（当前成分，存在幸存者偏差）", flush=True)
    if args.limit:
        symbols = symbols[: args.limit]

    # 2) 并发抓取
    rows = []
    fails = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, c, args.start, args.end): c for c in symbols}
        done = 0
        for fut in as_completed(futures):
            code, result = fut.result()
            done += 1
            if isinstance(result, Exception):
                fails.append((code, str(result)[:80]))
            else:
                rows.append(result)
            if done % 50 == 0:
                print(f"  进度: {done}/{len(symbols)} "
                      f"({time.time() - t0:.0f}s)", flush=True)

    ohlcv = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    ohlcv.to_csv(f"{args.out}/ohlcv.csv", index=False)
    print(f"OHLCV 已保存: {args.out}/ohlcv.csv ({ohlcv.shape}, "
          f"股票 {ohlcv['symbol'].nunique() if not ohlcv.empty else 0} 只, "
          f"用时 {time.time() - t0:.0f}s)", flush=True)
    if fails:
        print(f"失败 {len(fails)} 只: {fails[:10]}", flush=True)

    # 3) CSI300 指数日线（基准，新浪）
    try:
        idx = ak.stock_zh_index_daily(symbol="sh000300")
        idx = idx.rename(columns={"date": "date", "close": "close"})
        idx["date"] = idx["date"].astype(str)
        idx = idx[(idx["date"] >= args.start[:4] + "-01-01") & (idx["date"] <= args.end[:4] + "-12-31")]
        idx[["date", "close"]].to_csv(f"{args.out}/benchmark.csv", index=False)
        print(f"指数基准已保存: {args.out}/benchmark.csv ({len(idx)} 天)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"指数抓取失败（回退等权基准）: {exc}", flush=True)


if __name__ == "__main__":
    main()
