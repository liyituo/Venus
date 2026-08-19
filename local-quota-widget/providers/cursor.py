"""Cursor Dashboard 用量（非官方接口，需 WorkosCursorSessionToken）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QuotaWindow:
    label: str
    used_percent: float | None = None
    reset_at: int | None = None
    detail: str = ""


@dataclass
class ProviderQuota:
    ok: bool
    title: str
    plan: str = ""
    windows: list[QuotaWindow] = field(default_factory=list)
    extra_lines: list[str] = field(default_factory=list)
    error: str = ""
    updated_at: str = ""


def _request(
    session_token: str,
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 20.0,
) -> Any:
    url = f"https://cursor.com{path}"
    headers = {
        "Cookie": f"WorkosCursorSessionToken={session_token}",
        "Origin": "https://cursor.com",
        "Accept": "application/json",
        "User-Agent": "local-quota-widget/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        if not raw.strip():
            return {}
        return json.loads(raw)


def _pick_percent(obj: Any, *keys: str) -> float | None:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        val = obj.get(key)
        if val is None:
            continue
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


def _format_reset(ts: int | None) -> str:
    if not ts:
        return ""
    from datetime import datetime

    try:
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def fetch_cursor_quota(session_token: str) -> ProviderQuota:
    token = (session_token or "").strip()
    if not token:
        return ProviderQuota(
            ok=False,
            title="Cursor",
            error="未配置 session token（设置 → 粘贴 WorkosCursorSessionToken）",
        )

    try:
        summary = _request(token, "GET", "/api/usage-summary")
        period = _request(token, "POST", "/api/dashboard/get-current-period-usage", {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (401, 403):
            return ProviderQuota(
                ok=False,
                title="Cursor",
                error="Session 已失效，请重新复制 WorkosCursorSessionToken",
            )
        return ProviderQuota(
            ok=False,
            title="Cursor",
            error=f"HTTP {exc.code}: {body or exc.reason}",
        )
    except urllib.error.URLError as exc:
        return ProviderQuota(ok=False, title="Cursor", error=f"网络错误: {exc.reason}")
    except json.JSONDecodeError:
        return ProviderQuota(ok=False, title="Cursor", error="Cursor 返回了非 JSON 数据")
    except TimeoutError:
        return ProviderQuota(ok=False, title="Cursor", error="请求超时")

    windows: list[QuotaWindow] = []
    extra: list[str] = []
    plan = ""

    if isinstance(summary, dict):
        individual = summary.get("individualUsage") or summary.get("individual_usage") or {}
        plan_obj = individual.get("plan") if isinstance(individual, dict) else {}
        if isinstance(plan_obj, dict):
            plan = str(plan_obj.get("name") or plan_obj.get("planName") or plan_obj.get("type") or "")
            auto_pct = _pick_percent(plan_obj, "autoPercentUsed", "auto_percent_used")
            api_pct = _pick_percent(plan_obj, "apiPercentUsed", "api_percent_used")
            if auto_pct is not None:
                windows.append(QuotaWindow("Auto 额度", auto_pct))
            if api_pct is not None:
                windows.append(QuotaWindow("API 额度", api_pct))
            on_demand = plan_obj.get("onDemand") or plan_obj.get("on_demand")
            if isinstance(on_demand, dict):
                used = on_demand.get("used") or on_demand.get("usedCents") or on_demand.get("used_cents")
                if used is not None:
                    extra.append(f"按需: {used}")

    if isinstance(period, dict):
        auto_pct = _pick_percent(period, "autoPercentUsed", "auto_percent_used")
        api_pct = _pick_percent(period, "apiPercentUsed", "api_percent_used")
        plan_usage = period.get("planUsage") or period.get("plan_usage")
        if auto_pct is not None and not any(w.label == "Auto 额度" for w in windows):
            windows.append(QuotaWindow("Auto 额度", auto_pct))
        if api_pct is not None and not any(w.label == "API 额度" for w in windows):
            windows.append(QuotaWindow("API 额度", api_pct))
        if isinstance(plan_usage, dict):
            included = plan_usage.get("includedSpendCents") or plan_usage.get("included_spend_cents")
            used = plan_usage.get("usedSpendCents") or plan_usage.get("used_spend_cents")
            if included is not None and used is not None:
                try:
                    pct = float(used) / float(included) * 100 if float(included) else None
                except (TypeError, ValueError, ZeroDivisionError):
                    pct = None
                if pct is not None:
                    windows.insert(
                        0,
                        QuotaWindow(
                            "本周期",
                            min(100.0, pct),
                            detail=f"${float(used)/100:.2f} / ${float(included)/100:.2f}",
                        ),
                    )

    if not windows:
        windows.append(QuotaWindow("本周期", None, detail="未能解析字段，请检查 Dashboard 是否改版"))

    from datetime import datetime

    return ProviderQuota(
        ok=True,
        title="Cursor",
        plan=plan,
        windows=windows[:3],
        extra_lines=extra,
        updated_at=datetime.now().strftime("%H:%M:%S"),
    )
