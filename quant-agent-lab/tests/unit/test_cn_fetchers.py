"""cn_fetchers 单元测试（mock HTTP / akshare，不触网）。"""

from __future__ import annotations

from unittest import mock

import pytest

from quant_agent.data.cn_fetchers import (
    CnDailyFrame,
    fetch_cn_daily,
    normalize_cn_sources,
)


def test_normalize_cn_sources_defaults():
    assert normalize_cn_sources(None)[0] == "gtimg"
    assert normalize_cn_sources("gtimg,ths") == ("gtimg", "ths")


def test_fetch_cn_daily_uses_first_success():
    frame = CnDailyFrame(
        source="ths",
        rows=(
            {
                "date": "2026-08-24",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 1000.0,
                "source": "ths",
            },
        ),
    )
    with mock.patch(
        "quant_agent.data.cn_fetchers._fetch_sina",
        side_effect=RuntimeError("blocked"),
    ), mock.patch("quant_agent.data.cn_fetchers._fetch_tx", return_value=CnDailyFrame("tx", ())):
        with mock.patch("quant_agent.data.cn_fetchers._fetch_gtimg", return_value=frame):
            got = fetch_cn_daily("600519", sources=("sina", "tx", "gtimg"))
    assert got.source == "ths"
    assert got.rows[0]["close"] == 10.5


def test_fetch_ths_parses_year_payload():
    from quant_agent.data.cn_fetchers import _fetch_ths

    payload = {
        "data": "20260820,10,11,9,10.5,1000,0,0,,,0;20260821,10.1,11.1,9.1,10.6,1100,0,0,,,0"
    }
    resp = mock.Mock()
    resp.text = f'quotebridge_v6_line_hs_600519_01_2026({__import__("json").dumps(payload)})'
    resp.raise_for_status = mock.Mock()
    with mock.patch("requests.get", return_value=resp):
        frame = _fetch_ths("600519", lookback=120, xq_token=None)
    assert frame.source == "ths"
    assert len(frame.rows) == 2
    assert frame.rows[-1]["date"] == "2026-08-21"
    assert frame.rows[-1]["close"] == 10.6


def test_fetch_gtimg_parses_qfqday():
    from quant_agent.data.cn_fetchers import _fetch_gtimg

    payload = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-08-21", "1291.5", "1272.83", "1291.5", "1272.01", "33472"],
                    ["2026-08-24", "1271.01", "1304.66", "1313.8", "1270.33", "48440"],
                ]
            }
        }
    }
    resp = mock.Mock()
    resp.json = mock.Mock(return_value=payload)
    resp.raise_for_status = mock.Mock()
    with mock.patch("requests.get", return_value=resp):
        frame = _fetch_gtimg("600519", lookback=120, xq_token=None)
    assert frame.source == "gtimg"
    assert frame.rows[-1]["date"] == "2026-08-24"
    assert frame.rows[-1]["close"] == 1304.66


def test_fetch_xq_requires_token():
    with pytest.raises(ValueError, match="雪球需配置"):
        fetch_cn_daily("600519", sources=("xq",), xq_token=None)
